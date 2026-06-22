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
# Test 1 — fetch_inbox_messages includes CATEGORY_UPDATES email
# ---------------------------------------------------------------------------


def test_fetch_inbox_messages_includes_category_updates():
    """Email with only CATEGORY_UPDATES (no INBOX label) must be returned."""
    service = _make_service({"CATEGORY_UPDATES": ["msg-updates-1"]})
    result = skill.fetch_inbox_messages(service, batch_size=10)
    ids = [m["id"] for m in result]
    assert "msg-updates-1" in ids, "CATEGORY_UPDATES message must appear in result"


# ---------------------------------------------------------------------------
# Test 2 — fetch_new_messages includes CATEGORY_PROMOTIONS email
# ---------------------------------------------------------------------------


def test_fetch_new_messages_includes_category_promotions():
    """Email with only CATEGORY_PROMOTIONS must be returned by fetch_new_messages."""
    service = _make_q_service({"label:category-promotions": ["msg-promo-1"]})
    result = skill.fetch_new_messages(service, since_epoch=None, batch_size=10)
    ids = [m["id"] for m in result]
    assert "msg-promo-1" in ids, "CATEGORY_PROMOTIONS message must appear in result"


# ---------------------------------------------------------------------------
# Test 3 — dedup: email with both INBOX and CATEGORY_UPDATES appears exactly once
# ---------------------------------------------------------------------------


def test_fetch_dedup_both_labels():
    """Email carrying both INBOX and CATEGORY_UPDATES must appear exactly once."""
    # Both label buckets return the same message ID
    service = _make_service(
        {
            "INBOX": ["msg-shared-1"],
            "CATEGORY_UPDATES": ["msg-shared-1"],
        }
    )
    result = skill.fetch_inbox_messages(service, batch_size=10)
    ids = [m["id"] for m in result]
    assert ids.count("msg-shared-1") == 1, "Duplicate message must be deduped to exactly one entry"
