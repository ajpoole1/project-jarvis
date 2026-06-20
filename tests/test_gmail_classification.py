"""Tests for gmail-cleanup classification fixes — A1 (atomic tag transition) and A2 (tier-2 routing)."""

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
_spec = importlib.util.spec_from_file_location("gmail_cleanup_skill_classification", _SKILL_PATH)
skill = importlib.util.module_from_spec(_spec)
sys.modules["gmail_cleanup_skill_classification"] = skill
_spec.loader.exec_module(skill)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LABEL_MAP = {
    "projects": "Label_projects",
    "none": "Label_none",
    "receipts": "Label_receipts",
    "other": "Label_other",
    "financial": "Label_financial",
    "security": "Label_security",
    "family": "Label_family",
    "bills": "Label_bills",
    "health": "Label_health",
    "job-search": "Label_jobsearch",
}

_ACTIVE_TAGS = list(_LABEL_MAP.keys())


def _make_db():
    """In-memory DB with tables used by cmd_drain_none."""
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
    con.execute("""
        CREATE TABLE gmail_tag_proposals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            definition      TEXT NOT NULL,
            rule_type       TEXT NOT NULL DEFAULT 'llm-criteria',
            rule_spec       TEXT NOT NULL DEFAULT '',
            example_msg_ids TEXT NOT NULL DEFAULT '[]',
            example_subject TEXT NOT NULL DEFAULT '',
            example_sender  TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'pending',
            created_at      TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE gmail_sender_rules (
            sender_email TEXT PRIMARY KEY,
            action       TEXT NOT NULL,
            confirmed    INTEGER NOT NULL DEFAULT 0,
            last_applied TEXT
        )
    """)
    con.commit()
    return con


def _seed_pending(con, msg_id="msg1", tag="none", sender="info@email.plaid.com"):
    con.execute(
        "INSERT OR REPLACE INTO gmail_pending_actions"
        " (msg_id, sender_email, sender_display, subject, action, tag, label_ids_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, sender, "Plaid", "Plaid update", "keep", tag, json.dumps(["INBOX", "Label_none"])),
    )
    con.commit()


def _make_mock_service(msg_id="msg1"):
    """Return a MagicMock Gmail service that returns one message with jarvis/none label."""
    svc = MagicMock()
    svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": msg_id}]
    }
    svc.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "labelIds": ["INBOX", "Label_none"],
        "payload": {
            "headers": [
                {"name": "From", "value": "info@email.plaid.com"},
                {"name": "Subject", "value": "Plaid update"},
            ]
        },
    }
    svc.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    return svc


# ---------------------------------------------------------------------------
# A1 — Atomic tag transition: DB update
# ---------------------------------------------------------------------------


def test_reclassify_none_updates_db_tag():
    """cmd_drain_none must overwrite gmail_pending_actions.tag when tier-2 resolves to a real tag."""
    con = _make_db()
    _seed_pending(con, tag="none")
    svc = _make_mock_service()

    with (
        patch.object(skill, "get_gmail_service", return_value=svc),
        patch.object(skill, "init_db", return_value=con),
        patch.object(skill, "get_or_create_labels", return_value=_LABEL_MAP),
        patch.object(skill, "_get_active_tags", return_value=_ACTIVE_TAGS),
        patch.object(skill, "fetch_body", return_value="Plaid banking update"),
        patch.object(
            skill,
            "_classify_tier2",
            return_value={"status": "existing_tag", "tag": "projects", "reason": "finance tool"},
        ),
    ):
        skill.cmd_drain_none([])

    row = con.execute("SELECT tag FROM gmail_pending_actions WHERE msg_id = 'msg1'").fetchone()
    assert row is not None, "pending row must still exist"
    assert row[0] == "projects", f"DB tag should be 'projects', got '{row[0]}'"


def test_reclassify_catchall_updates_db_tag():
    """cmd_drain_none must update gmail_pending_actions.tag to 'other' for catch-all resolution."""
    con = _make_db()
    _seed_pending(con, tag="none")
    svc = _make_mock_service()

    with (
        patch.object(skill, "get_gmail_service", return_value=svc),
        patch.object(skill, "init_db", return_value=con),
        patch.object(skill, "get_or_create_labels", return_value=_LABEL_MAP),
        patch.object(skill, "_get_active_tags", return_value=_ACTIVE_TAGS),
        patch.object(skill, "fetch_body", return_value="unclassifiable content"),
        patch.object(
            skill,
            "_classify_tier2",
            return_value={"status": "catchall", "reason": "nothing fits"},
        ),
    ):
        skill.cmd_drain_none([])

    row = con.execute("SELECT tag FROM gmail_pending_actions WHERE msg_id = 'msg1'").fetchone()
    assert row is not None
    assert row[0] == skill.OTHER_TAG, f"DB tag should be '{skill.OTHER_TAG}', got '{row[0]}'"


def test_reclassify_none_removes_none_gmail_label():
    """execute_actions must remove jarvis/none label when a real tag is applied."""
    con = _make_db()
    _seed_pending(con, tag="none")
    svc = _make_mock_service()

    with (
        patch.object(skill, "get_gmail_service", return_value=svc),
        patch.object(skill, "init_db", return_value=con),
        patch.object(skill, "get_or_create_labels", return_value=_LABEL_MAP),
        patch.object(skill, "_get_active_tags", return_value=_ACTIVE_TAGS),
        patch.object(skill, "fetch_body", return_value="Plaid banking update"),
        patch.object(
            skill,
            "_classify_tier2",
            return_value={"status": "existing_tag", "tag": "projects", "reason": "finance tool"},
        ),
    ):
        skill.cmd_drain_none([])

    modify_mock = svc.users.return_value.messages.return_value.modify
    assert modify_mock.called, "modify must be called to apply the new label"
    body_arg = modify_mock.call_args.kwargs.get("body", modify_mock.call_args[1].get("body", {}))
    remove_ids = body_arg.get("removeLabelIds", [])
    assert (
        "Label_none" in remove_ids
    ), f"jarvis/none label must be in removeLabelIds; got {remove_ids}"


# ---------------------------------------------------------------------------
# A2 — Tier-2 prompt includes project domain signals
# ---------------------------------------------------------------------------


def test_classify_tier2_prompt_includes_plaid_signal():
    """_classify_tier2 system prompt must include plaid.com signal for projects routing."""
    real_tags = ["receipts", "financial", "projects", "other", "security"]

    captured = []

    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text='{"status": "existing_tag", "tag": "projects", "reason": "Plaid is a finance tool"}'
        )
    ]

    def _capture_create(**kwargs):
        captured.append(kwargs.get("system", ""))
        return mock_response

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _capture_create

    with patch.object(skill, "anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = mock_client
        skill._classify_tier2(
            body="Your Plaid connection was updated.",
            subject="Plaid update",
            sender_email="info@email.plaid.com",
            real_tags=real_tags,
        )

    assert captured, "_classify_tier2 must call the Anthropic client"
    system_prompt = captured[0].lower()
    assert "plaid" in system_prompt, "tier-2 prompt must include 'plaid' domain signal"
    assert "projects" in system_prompt, "tier-2 prompt must reference 'projects' tag for Plaid"


def test_classify_tier2_prompt_includes_expo_signal():
    """_classify_tier2 system prompt must include expo.dev signal for projects routing."""
    real_tags = ["projects", "other"]

    captured = []

    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text='{"status": "existing_tag", "tag": "projects", "reason": "Expo is a dev tool"}'
        )
    ]

    def _capture_create(**kwargs):
        captured.append(kwargs.get("system", ""))
        return mock_response

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _capture_create

    with patch.object(skill, "anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = mock_client
        skill._classify_tier2(
            body="Your Expo build is ready.",
            subject="Expo build",
            sender_email="hello@expo.dev",
            real_tags=real_tags,
        )

    assert captured
    system_prompt = captured[0].lower()
    assert "expo" in system_prompt


# ---------------------------------------------------------------------------
# A2 — Tier-1 prompt must not show 'other' as a classifiable tag
# ---------------------------------------------------------------------------


def test_tier1_tag_definitions_excludes_other():
    """_build_tag_definitions must not expose 'other' to tier-1 — only tier-2 assigns it."""
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE gmail_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            definition TEXT NOT NULL,
            rule_type TEXT NOT NULL DEFAULT 'llm-criteria',
            rule_spec TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    for name, defn in [
        ("receipts", "Purchase receipts"),
        ("projects", "Dev tools"),
        ("other", "Catch-all"),
    ]:
        con.execute(
            "INSERT INTO gmail_tags (name, definition, created_at) VALUES (?, ?, '2026-01-01')",
            (name, defn),
        )
    con.commit()

    definitions = skill._build_tag_definitions(con)

    assert "other" not in definitions, "'other' must not appear in tier-1 tag definitions block"
    assert "receipts" in definitions
    assert "projects" in definitions


def test_classify_emails_forces_other_to_none():
    """classify_emails must convert any tier-1 'other' tag to 'none' for tier-2 routing."""
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text='[{"action": "keep", "tag": "other", "reason": "no match", "calendar_hint": false, "uncertain": false}]'
        )
    ]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE gmail_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            definition TEXT NOT NULL,
            rule_type TEXT NOT NULL DEFAULT 'llm-criteria',
            rule_spec TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE gmail_sender_rules (
            sender_email TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            confirmed INTEGER NOT NULL DEFAULT 0,
            last_applied TEXT
        )
    """)
    for name, defn in [
        ("receipts", "Purchase"),
        ("projects", "Dev"),
        ("other", "Catch-all"),
        ("none", "Transient"),
    ]:
        con.execute(
            "INSERT INTO gmail_tags (name, definition, created_at) VALUES (?, ?, '2026-01-01')",
            (name, defn),
        )
    con.commit()

    emails = [
        {
            "id": "msg1",
            "payload": {
                "headers": [
                    {"name": "From", "value": "unknown@example.com"},
                    {"name": "Subject", "value": "Random"},
                ]
            },
            "snippet": "some content",
            "labelIds": [],
        }
    ]

    with patch.object(skill, "anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = mock_client
        with patch.object(skill, "get_cached_action", return_value=None):
            with patch.object(skill, "cache_rule"):
                with patch.object(skill, "fetch_calendar_context", return_value=""):
                    results = skill.classify_emails(emails, con)

    assert (
        results[0].tag == "none"
    ), f"tier-1 'other' must be converted to 'none'; got '{results[0].tag}'"
