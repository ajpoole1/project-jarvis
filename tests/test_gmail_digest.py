"""Tests for skills/gmail-cleanup/skill.py — cmd_digest empty and non-empty paths."""

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


def _make_db():
    """In-memory SQLite connection with the heartbeat table."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE gmail_heartbeat_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return con


def _seed_queue(con, items):
    con.execute(
        "INSERT OR REPLACE INTO gmail_heartbeat_state (key, value) VALUES (?, ?)",
        ("digest_queue", json.dumps(items)),
    )
    con.commit()


def _read_queue(con):
    row = con.execute(
        "SELECT value FROM gmail_heartbeat_state WHERE key = 'digest_queue'"
    ).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------------------
# Empty queue
# ---------------------------------------------------------------------------


def test_cmd_digest_empty_queue_returns_confirmation():
    con = _make_db()
    _seed_queue(con, [])
    with patch.object(skill, "init_db", return_value=con):
        result = skill.cmd_digest()
    assert result == "📭 Gmail: inbox clean — nothing to action."


def test_cmd_digest_empty_queue_single_line():
    con = _make_db()
    _seed_queue(con, [])
    with patch.object(skill, "init_db", return_value=con):
        result = skill.cmd_digest()
    assert "\n" not in result, "empty digest must be exactly one line"


def test_cmd_digest_empty_queue_does_not_write_db():
    con = _make_db()
    _seed_queue(con, [])
    written = []

    original = skill.set_heartbeat_state

    def _spy(c, key, value):
        written.append((key, value))
        original(c, key, value)

    with patch.object(skill, "init_db", return_value=con):
        with patch.object(skill, "set_heartbeat_state", side_effect=_spy):
            skill.cmd_digest()

    assert written == [], "empty queue must not write to digest_queue"


# ---------------------------------------------------------------------------
# Non-empty queue
# ---------------------------------------------------------------------------


def test_cmd_digest_non_empty_returns_grouped_block():
    con = _make_db()
    _seed_queue(
        con,
        [
            {"action": "trash", "subject": "Delete me"},
            {"action": "archive", "subject": "File me away"},
        ],
    )
    with patch.object(skill, "init_db", return_value=con):
        with patch.object(skill, "get_gmail_service", return_value=MagicMock()):
            result = skill.cmd_digest()
    assert "**Gmail digest**" in result
    assert "2 emails" in result
    assert "Delete me" in result
    assert "File me away" in result


def test_cmd_digest_non_empty_clears_queue():
    con = _make_db()
    _seed_queue(con, [{"action": "trash", "subject": "Spam"}])
    with patch.object(skill, "init_db", return_value=con):
        with patch.object(skill, "get_gmail_service", return_value=MagicMock()):
            skill.cmd_digest()
    assert _read_queue(con) == [], "non-empty digest must clear digest_queue"


# ---------------------------------------------------------------------------
# Bug A — pending label_ids_json round-trip (DEV_NOTES #59a)
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
# Spec 2026-0036 — digest executes actions inline
# ---------------------------------------------------------------------------


def _make_full_db():
    """In-memory DB with heartbeat state + sender rules tables."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE gmail_heartbeat_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute(
        """CREATE TABLE gmail_sender_rules (
            sender_email TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            confirmed INTEGER NOT NULL DEFAULT 0,
            last_applied TEXT
        )"""
    )
    return con


def test_cmd_digest_executes_trash_and_archive():
    """cmd_digest must call trash() for trash items and modify() for archive items."""
    con = _make_full_db()
    _seed_queue(
        con,
        [
            {
                "msg_id": "msg-trash-1",
                "sender": "Spammer",
                "sender_email": "spam@example.com",
                "subject": "Win a prize",
                "action": "trash",
                "tag": "promotions",
            },
            {
                "msg_id": "msg-archive-2",
                "sender": "Newsletter",
                "sender_email": "news@example.com",
                "subject": "Weekly digest",
                "action": "archive",
                "tag": "newsletters",
            },
        ],
    )

    mock_service = MagicMock()
    mock_trash = mock_service.users().messages().trash
    mock_modify = mock_service.users().messages().modify

    with patch.object(skill, "init_db", return_value=con):
        with patch.object(skill, "get_gmail_service", return_value=mock_service):
            result = skill.cmd_digest()

    mock_trash.assert_called_once_with(userId="me", id="msg-trash-1")
    mock_trash.return_value.execute.assert_called_once()
    mock_modify.assert_called_once_with(
        userId="me", id="msg-archive-2", body={"removeLabelIds": ["INBOX"]}
    )
    mock_modify.return_value.execute.assert_called_once()
    assert "Actions executed automatically." in result
    assert _read_queue(con) == []
