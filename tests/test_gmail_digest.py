"""Tests for skills/gmail-cleanup/skill.py — cmd_digest and related functions."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Mock heavy third-party deps before importing the skill module.
# ---------------------------------------------------------------------------

_MOCK_PKGS = (
    "anthropic",
    "dotenv",
    "google",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.credentials",
    "google_auth_oauthlib",
    "google_auth_oauthlib.flow",
    "googleapiclient",
    "googleapiclient.discovery",
)
for _pkg in _MOCK_PKGS:
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "gmail-cleanup" / "skill.py"
_spec = importlib.util.spec_from_file_location("gmail_cleanup_skill", _SKILL_PATH)
skill = importlib.util.module_from_spec(_spec)
sys.modules["gmail_cleanup_skill"] = skill
_spec.loader.exec_module(skill)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pending_db():
    """In-memory DB with the tables needed for save_pending / load_pending."""
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE gmail_pending_actions (
            msg_id          TEXT PRIMARY KEY,
            sender_email    TEXT NOT NULL,
            sender_display  TEXT NOT NULL,
            subject         TEXT NOT NULL,
            action          TEXT NOT NULL,
            tag             TEXT NOT NULL DEFAULT 'none',
            reason          TEXT,
            staged_at       TEXT DEFAULT (datetime('now')),
            label_ids_json  TEXT NOT NULL DEFAULT '[]'
        )
    """)
    return con


def _make_kv_db():
    """In-memory DB with jarvis_kv (suspended flag set) and gmail_sender_rules."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE jarvis_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("""CREATE TABLE gmail_sender_rules (
        sender_email TEXT PRIMARY KEY,
        action TEXT NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0,
        last_applied TEXT
    )""")
    con.execute("INSERT INTO jarvis_kv VALUES ('gmail_actions_suspended', '1')")
    con.commit()
    return con


def _make_hb_db():
    """In-memory DB with tables needed for cmd_heartbeat tests."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE gmail_heartbeat_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("""
        CREATE TABLE gmail_watches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            label       TEXT NOT NULL,
            description TEXT NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    con.commit()
    return con


def _make_summary(msg_id, sender, action):
    return skill.EmailSummary(
        msg_id=msg_id,
        sender=sender,
        sender_email=f"{sender.lower().replace(' ', '')}@example.com",
        subject=f"Subject from {sender}",
        action=action,
        reason="test",
    )


# ---------------------------------------------------------------------------
# Bug A — pending label_ids_json round-trip (DEV_NOTES #59a)
# ---------------------------------------------------------------------------


def test_save_pending_persists_label_ids():
    """save_pending must serialise current_label_ids into label_ids_json."""
    con = _make_pending_db()
    summary = skill.EmailSummary(
        msg_id="msg1",
        sender="Sender",
        sender_email="sender@example.com",
        subject="Test",
        action="archive",
        reason="test",
        tag="receipts",
        current_label_ids=["INBOX", "Label_none_id"],
    )
    skill.save_pending(con, [summary])
    row = con.execute(
        "SELECT label_ids_json FROM gmail_pending_actions WHERE msg_id = 'msg1'"
    ).fetchone()
    assert row is not None
    assert json.loads(row[0]) == ["INBOX", "Label_none_id"]


def test_load_pending_restores_label_ids():
    """load_pending must deserialise label_ids_json back into current_label_ids."""
    con = _make_pending_db()
    con.execute(
        "INSERT INTO gmail_pending_actions"
        " (msg_id, sender_email, sender_display, subject, action, tag, reason, label_ids_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("msg2", "s@e.com", "S", "Subj", "archive", "receipts", "", '["INBOX","Label_none"]'),
    )
    con.commit()
    summaries = skill.load_pending(con)
    assert len(summaries) == 1
    assert summaries[0].current_label_ids == ["INBOX", "Label_none"]


def test_load_pending_missing_column_falls_back_to_empty():
    """load_pending on a DB without label_ids_json must not crash (migration guard)."""
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE gmail_pending_actions (
            msg_id          TEXT PRIMARY KEY,
            sender_email    TEXT NOT NULL,
            sender_display  TEXT NOT NULL,
            subject         TEXT NOT NULL,
            action          TEXT NOT NULL,
            tag             TEXT NOT NULL DEFAULT 'none',
            reason          TEXT,
            staged_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute(
        "INSERT INTO gmail_pending_actions"
        " (msg_id, sender_email, sender_display, subject, action, tag)"
        " VALUES ('m1','s@e.com','S','Sub','archive','receipts')"
    )
    con.commit()
    # ALTER-table migration adds the column; simulate that it ran
    con.execute(
        "ALTER TABLE gmail_pending_actions ADD COLUMN label_ids_json TEXT NOT NULL DEFAULT '[]'"
    )
    con.commit()
    summaries = skill.load_pending(con)
    assert summaries[0].current_label_ids == []


# ---------------------------------------------------------------------------
# Bug B — tier-1 prompt routes uncertain to 'none', not 'other' (DEV_NOTES #58)
# ---------------------------------------------------------------------------


def test_tier1_prompt_instructs_none_for_uncertain():
    """The tier-1 system prompt must instruct the LLM to use 'none' for uncertain items."""
    prompt = skill._build_classifier_system_prompt("receipts: purchase emails", "", "")
    assert "none" in prompt.lower()
    assert "uncertain" in prompt.lower()


def test_tier1_prompt_does_not_assign_other_directly():
    """The tier-1 prompt must NOT tell the LLM to freely assign 'other' — tier-2 does that."""
    prompt = skill._build_classifier_system_prompt("receipts: purchase emails", "", "")
    assert (
        "do not assign 'other' directly" in prompt.lower()
        or "do not assign other directly" in prompt.lower()
        or "only by tier-2" in prompt.lower()
    )


# ---------------------------------------------------------------------------
# Spec 2026-0038 — execute_actions suspended flag
# ---------------------------------------------------------------------------


def test_execute_actions_suspended():
    """execute_actions must return early with no API writes when suspended flag is set."""
    con = _make_kv_db()
    mock_service = MagicMock()
    summary = skill.EmailSummary(
        msg_id="m1",
        sender="S",
        sender_email="s@e.com",
        subject="Sub",
        action="trash",
        reason="test",
    )
    result = skill.execute_actions(mock_service, [summary], con)
    assert result is not None
    assert "suspended" in result.lower()
    mock_service.users.assert_not_called()


# ---------------------------------------------------------------------------
# Spec 2026-0039 — cmd_digest as full inbox report
# ---------------------------------------------------------------------------


def test_cmd_digest_includes_act_items():
    """Digest must show act-tier emails under ACT — inbox (replaces legacy KEEP section)."""
    summary = _make_summary("m1", "Alice", "keep")
    summary.tier = "act"
    summary.disposition = "inbox"
    with (
        patch.object(skill, "get_gmail_service", return_value=MagicMock()),
        patch.object(skill, "init_db", return_value=MagicMock()),
        patch.object(skill, "fetch_inbox_messages", return_value=[{"id": "m1"}]),
        patch.object(skill, "classify_emails", return_value=[summary]),
        patch.object(skill, "save_pending"),
    ):
        result = skill.cmd_digest()
    assert "ACT" in result
    assert "Alice" in result


def test_cmd_digest_empty_inbox():
    """Digest must return inbox-empty string when fetch returns no messages."""
    with (
        patch.object(skill, "get_gmail_service", return_value=MagicMock()),
        patch.object(skill, "init_db", return_value=MagicMock()),
        patch.object(skill, "fetch_inbox_messages", return_value=[]),
    ):
        result = skill.cmd_digest()
    assert "inbox empty" in result


def test_cmd_digest_calls_save_pending():
    """Digest must call save_pending so cmd_execute has data to act on."""
    summary = _make_summary("m1", "Bob", "archive")
    with (
        patch.object(skill, "get_gmail_service", return_value=MagicMock()),
        patch.object(skill, "init_db", return_value=MagicMock()),
        patch.object(skill, "fetch_inbox_messages", return_value=[{"id": "m1"}]),
        patch.object(skill, "classify_emails", return_value=[summary]),
        patch.object(skill, "save_pending") as mock_save,
    ):
        skill.cmd_digest()
    mock_save.assert_called_once()


def test_cmd_digest_all_tier_buckets():
    """Digest output includes all non-empty tier buckets (ACT/AWARE/ARCHIVE)."""
    act_s = _make_summary("m1", "Alice", "keep")
    act_s.tier = "act"
    act_s.disposition = "inbox"
    aware_s = _make_summary("m2", "Newsletter", "archive")
    aware_s.tier = "aware"
    aware_s.disposition = "file"
    archive_s = _make_summary("m3", "Spammer", "archive")
    archive_s.tier = "archive"
    archive_s.disposition = "quarantine"
    summaries = [act_s, aware_s, archive_s]
    with (
        patch.object(skill, "get_gmail_service", return_value=MagicMock()),
        patch.object(skill, "init_db", return_value=MagicMock()),
        patch.object(
            skill, "fetch_inbox_messages", return_value=[{"id": s.msg_id} for s in summaries]
        ),
        patch.object(skill, "classify_emails", return_value=summaries),
        patch.object(skill, "save_pending"),
    ):
        result = skill.cmd_digest()
    assert "ACT" in result
    assert "AWARE" in result
    assert "ARCHIVE" in result


def test_cmd_digest_header_counts_act_aware_only():
    """Header 'in inbox' count must be act+aware only; archive is reported separately."""
    act_s = _make_summary("m1", "Alice", "keep")
    act_s.tier = "act"
    act_s.disposition = "inbox"
    aware_s = _make_summary("m2", "Newsletter", "archive")
    aware_s.tier = "aware"
    aware_s.disposition = "file"
    archive_a = _make_summary("m3", "Spammer", "archive")
    archive_a.tier = "archive"
    archive_a.disposition = "file"
    archive_b = _make_summary("m4", "Promo", "archive")
    archive_b.tier = "archive"
    archive_b.disposition = "file"
    summaries = [act_s, aware_s, archive_a, archive_b]
    with (
        patch.object(skill, "get_gmail_service", return_value=MagicMock()),
        patch.object(skill, "init_db", return_value=MagicMock()),
        patch.object(
            skill, "fetch_inbox_messages", return_value=[{"id": s.msg_id} for s in summaries]
        ),
        patch.object(skill, "classify_emails", return_value=summaries),
        patch.object(skill, "save_pending"),
    ):
        result = skill.cmd_digest()
    # 1 act + 1 aware = 2 in inbox; 2 archive reported separately
    assert "2 in inbox" in result
    assert "2 to archive" in result
    assert "4 emails in inbox" not in result  # old misleading total must be gone


def test_cmd_heartbeat_no_digest_queue_writes():
    """Heartbeat must never call set_heartbeat_state with key 'digest_queue'."""
    con = _make_hb_db()
    con.execute(
        "INSERT INTO gmail_heartbeat_state (key, value) VALUES ('last_checked', '2026-01-01T00:00:00+00:00')"
    )
    con.commit()

    # Non-priority archive email — would have gone to digest_items in the old code.
    non_priority = skill.EmailSummary(
        msg_id="m1",
        sender="Newsletter",
        sender_email="news@example.com",
        subject="Weekly news",
        action="archive",
        reason="newsletter",
        tag="other",
    )

    written_keys: list[str] = []
    original_set = skill.set_heartbeat_state

    def _spy(c, key, value):
        written_keys.append(key)
        original_set(c, key, value)

    with (
        patch.object(skill, "get_gmail_service", return_value=MagicMock()),
        patch.object(skill, "init_db", return_value=con),
        patch.object(skill, "fetch_new_messages", return_value=[{"id": "m1"}]),
        patch.object(skill, "classify_emails", return_value=[non_priority]),
        patch.object(skill, "set_heartbeat_state", side_effect=_spy),
    ):
        skill.cmd_heartbeat()

    assert (
        "digest_queue" not in written_keys
    ), f"heartbeat must not write to digest_queue; keys written: {written_keys}"


# ---------------------------------------------------------------------------
# Bug fix — fetch_inbox_messages must not return archived category-tab mail
# ---------------------------------------------------------------------------


def test_fetch_inbox_messages_uses_in_inbox_query():
    """fetch_inbox_messages must query with q='in:inbox', not bare labelIds per category."""
    mock_service = MagicMock()
    mock_list = mock_service.users.return_value.messages.return_value.list
    mock_list.return_value.execute.return_value = {"messages": []}

    skill.fetch_inbox_messages(mock_service, batch_size=10)

    call_kwargs = mock_list.call_args_list
    assert call_kwargs, "messages().list() was never called"
    for call in call_kwargs:
        kwargs = call.kwargs if call.kwargs else call[1]
        assert "in:inbox" in kwargs.get("q", ""), f"Expected q containing 'in:inbox', got: {kwargs}"
        assert (
            "labelIds" not in kwargs
        ), f"fetch_inbox_messages must not pass bare labelIds; got: {kwargs}"


def test_fetch_inbox_messages_archived_promo_not_returned():
    """Archived promo messages (CATEGORY_PROMOTIONS but no INBOX) must not be staged."""
    mock_service = MagicMock()
    mock_list = mock_service.users.return_value.messages.return_value.list

    def _list_side_effect(**kwargs):
        mock_result = MagicMock()
        if "in:inbox" in kwargs.get("q", ""):
            mock_result.execute.return_value = {"messages": []}
        else:
            mock_result.execute.return_value = {"messages": [{"id": "archived-promo-123"}]}
        return mock_result

    mock_list.side_effect = _list_side_effect

    result = skill.fetch_inbox_messages(mock_service, batch_size=10)
    ids = [m["id"] for m in result]
    assert "archived-promo-123" not in ids, "Archived promo message must not appear in inbox fetch"
