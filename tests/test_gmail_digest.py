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
    with patch.object(skill, "init_db", return_value=_make_db()):
        result = skill.cmd_digest()
    assert result == "📭 Gmail: inbox clean — nothing to action."


def test_cmd_digest_empty_queue_single_line():
    with patch.object(skill, "init_db", return_value=_make_db()):
        result = skill.cmd_digest()
    assert "\n" not in result, "empty digest must be exactly one line"


def test_cmd_digest_empty_queue_does_not_write_db():
    con = _make_db()
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
        result = skill.cmd_digest()
    assert "**Gmail digest**" in result
    assert "2 emails" in result
    assert "Delete me" in result
    assert "File me away" in result


def test_cmd_digest_non_empty_clears_queue():
    con = _make_db()
    _seed_queue(con, [{"action": "trash", "subject": "Spam"}])
    with patch.object(skill, "init_db", return_value=con):
        skill.cmd_digest()
    assert _read_queue(con) == [], "non-empty digest must clear digest_queue"
