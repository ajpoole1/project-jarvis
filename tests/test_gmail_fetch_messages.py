"""Tests for fetch_inbox_messages and fetch_new_messages — spec 2026-0037."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

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
_spec = importlib.util.spec_from_file_location("gmail_cleanup_skill_fetch", _SKILL_PATH)
skill = importlib.util.module_from_spec(_spec)
sys.modules["gmail_cleanup_skill_fetch"] = skill
_spec.loader.exec_module(skill)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(label_to_ids: dict[str, list[str]]) -> MagicMock:
    """Build a mock Gmail service that returns the given message IDs per label.

    label_to_ids: maps a label string to a list of message IDs returned for that label.
    Calls using labelIds=[label] return those IDs; unknown labels return empty.
    Each message.get() call returns a minimal metadata dict keyed by id.
    """
    service = MagicMock()

    def _list_side_effect(**kwargs):
        label = (kwargs.get("labelIds") or [None])[0]
        ids = label_to_ids.get(label, [])
        mock_result = MagicMock()
        mock_result.execute.return_value = {
            "messages": [{"id": mid} for mid in ids],
        }
        return mock_result

    service.users().messages().list.side_effect = _list_side_effect

    def _get_side_effect(**kwargs):
        mid = kwargs["id"]
        mock_result = MagicMock()
        mock_result.execute.return_value = {
            "id": mid,
            "labelIds": [],
            "payload": {"headers": []},
        }
        return mock_result

    service.users().messages().get.side_effect = _get_side_effect
    return service


def _make_q_service(q_to_ids: dict[str, list[str]]) -> MagicMock:
    """Build a mock Gmail service that returns message IDs based on query string."""
    service = MagicMock()

    def _list_side_effect(**kwargs):
        q = kwargs.get("q", "")
        # Match any query that starts with the registered key, for flexibility
        ids = []
        for key, val in q_to_ids.items():
            if key in q:
                ids = val
                break
        mock_result = MagicMock()
        mock_result.execute.return_value = {
            "messages": [{"id": mid} for mid in ids],
        }
        return mock_result

    service.users().messages().list.side_effect = _list_side_effect

    def _get_side_effect(**kwargs):
        mid = kwargs["id"]
        mock_result = MagicMock()
        mock_result.execute.return_value = {
            "id": mid,
            "labelIds": [],
            "payload": {"headers": []},
        }
        return mock_result

    service.users().messages().get.side_effect = _get_side_effect
    return service


# ---------------------------------------------------------------------------
# Test 1 — fetch_inbox_messages uses in:inbox query
# ---------------------------------------------------------------------------


def test_fetch_inbox_messages_uses_inbox_query():
    """fetch_inbox_messages must query with q='in:inbox', not label filters."""
    service = _make_q_service({"in:inbox": ["msg-inbox-1", "msg-inbox-2"]})
    result = skill.fetch_inbox_messages(service, batch_size=10)
    ids = [m["id"] for m in result]
    assert "msg-inbox-1" in ids
    assert "msg-inbox-2" in ids


# ---------------------------------------------------------------------------
# Test 2 — fetch_new_messages uses in:inbox query
# ---------------------------------------------------------------------------


def test_fetch_new_messages_uses_inbox_query():
    """fetch_new_messages must use in:inbox (not label OR clauses)."""
    service = _make_q_service({"in:inbox": ["msg-new-1"]})
    result = skill.fetch_new_messages(service, since_epoch=None, batch_size=10)
    ids = [m["id"] for m in result]
    assert "msg-new-1" in ids


# ---------------------------------------------------------------------------
# Test 3 — fetch_new_messages appends after: filter when since_epoch given
# ---------------------------------------------------------------------------


def test_fetch_new_messages_appends_after_epoch():
    """When since_epoch is set, the query must include 'after:<epoch>'."""
    captured = {}

    def _list_side_effect(**kwargs):
        captured["q"] = kwargs.get("q", "")
        mock_result = MagicMock()
        mock_result.execute.return_value = {"messages": []}
        return mock_result

    service = MagicMock()
    service.users().messages().list.side_effect = _list_side_effect
    skill.fetch_new_messages(service, since_epoch=1700000000, batch_size=10)
    assert "in:inbox" in captured["q"]
    assert "after:1700000000" in captured["q"]
