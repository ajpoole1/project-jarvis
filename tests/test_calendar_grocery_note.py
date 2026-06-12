"""Tests for calendar skill Part A additions — grocery note commands (2026-0017).

Covers: cmd_grocery_event, cmd_get_notes, cmd_set_notes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Load calendar module (mock Google API imports)
# ---------------------------------------------------------------------------

_CALENDAR_PATH = Path(__file__).parents[1] / "skills" / "calendar" / "skill.py"


def _load_calendar():
    sys.modules.setdefault("dotenv", MagicMock())
    sys.modules.setdefault("google", MagicMock())
    sys.modules.setdefault("google.auth", MagicMock())
    sys.modules.setdefault("google.auth.transport", MagicMock())
    sys.modules.setdefault("google.auth.transport.requests", MagicMock())
    sys.modules.setdefault("google.oauth2", MagicMock())
    sys.modules.setdefault("google.oauth2.credentials", MagicMock())
    sys.modules.setdefault("google_auth_oauthlib", MagicMock())
    sys.modules.setdefault("google_auth_oauthlib.flow", MagicMock())
    sys.modules.setdefault("googleapiclient", MagicMock())
    sys.modules.setdefault("googleapiclient.discovery", MagicMock())
    spec = importlib.util.spec_from_file_location("calendar_skill_v2", _CALENDAR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cal = _load_calendar()

cmd_grocery_event = _cal.cmd_grocery_event
cmd_get_notes = _cal.cmd_get_notes
cmd_set_notes = _cal.cmd_set_notes
_paginate_events = _cal._paginate_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service_with_events(items: list[dict]) -> MagicMock:
    resp = {"items": items}
    execute_mock = MagicMock(return_value=resp)
    list_mock = MagicMock(return_value=MagicMock(execute=execute_mock))
    svc = MagicMock()
    svc.events.return_value.list = list_mock
    return svc


def _grocery_event(description: str = "milk\nspinach") -> dict:
    return {
        "id": "evt_grocery_001",
        "summary": "Grocery list",
        "start": {"date": "2026-06-01"},
        "end": {"date": "2026-06-02"},
        "description": description,
    }


# ===========================================================================
# cmd_grocery_event
# ===========================================================================


class TestCmdGroceryEvent:
    def test_returns_event_when_found(self, monkeypatch):
        svc = _make_service_with_events([_grocery_event("peaches\nrice")])
        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal_123")
        monkeypatch.setattr(
            _cal,
            "_paginate_events",
            lambda *a, **kw: ([_grocery_event("peaches\nrice")], False),
        )

        raw = cmd_grocery_event(svc)
        data = json.loads(raw)

        assert data["found"] is True
        assert data["event_id"] == "evt_grocery_001"
        assert "peaches" in data["description"]

    def test_not_found_when_no_matching_event(self, monkeypatch):
        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal_123")
        monkeypatch.setattr(_cal, "_paginate_events", lambda *a, **kw: ([], False))
        svc = MagicMock()

        raw = cmd_grocery_event(svc)
        data = json.loads(raw)

        assert data["found"] is False
        assert "error" in data

    def test_case_insensitive_summary_match(self, monkeypatch):
        event = dict(_grocery_event())
        event["summary"] = "GROCERY LIST"
        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal_123")
        monkeypatch.setattr(_cal, "_paginate_events", lambda *a, **kw: ([event], False))
        svc = MagicMock()

        raw = cmd_grocery_event(svc)
        data = json.loads(raw)

        assert data["found"] is True

    def test_no_home_calendar_id_returns_error(self, monkeypatch):
        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "")
        svc = MagicMock()

        raw = cmd_grocery_event(svc)
        data = json.loads(raw)

        assert data["found"] is False

    def test_skips_non_grocery_events(self, monkeypatch):
        non_grocery = {"id": "x", "summary": "Doctor appointment", "start": {"date": "2026-06-01"}}
        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal_123")
        monkeypatch.setattr(_cal, "_paginate_events", lambda *a, **kw: ([non_grocery], False))
        svc = MagicMock()

        raw = cmd_grocery_event(svc)
        data = json.loads(raw)

        assert data["found"] is False

    def test_empty_description_returns_empty_string(self, monkeypatch):
        event = dict(_grocery_event())
        event["description"] = None
        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal_123")
        monkeypatch.setattr(_cal, "_paginate_events", lambda *a, **kw: ([event], False))
        svc = MagicMock()

        raw = cmd_grocery_event(svc)
        data = json.loads(raw)

        assert data["description"] == ""


# ===========================================================================
# cmd_get_notes
# ===========================================================================


class TestCmdGetNotes:
    def test_returns_description(self, monkeypatch):
        event = {"description": "milk\nspinach", "id": "evt123"}
        get_mock = MagicMock(return_value=MagicMock(execute=MagicMock(return_value=event)))
        svc = MagicMock()
        svc.events.return_value.get = get_mock

        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal")
        result = cmd_get_notes(svc, "evt123")

        assert result == "milk\nspinach"

    def test_returns_empty_string_when_no_description(self, monkeypatch):
        event = {"id": "evt123"}
        get_mock = MagicMock(return_value=MagicMock(execute=MagicMock(return_value=event)))
        svc = MagicMock()
        svc.events.return_value.get = get_mock

        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal")
        result = cmd_get_notes(svc, "evt123")

        assert result == ""

    def test_no_home_calendar_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "")
        svc = MagicMock()

        result = cmd_get_notes(svc, "evt123")

        assert result == ""

    def test_api_error_returns_error_string(self, monkeypatch):
        get_mock = MagicMock(
            return_value=MagicMock(execute=MagicMock(side_effect=Exception("403 Forbidden")))
        )
        svc = MagicMock()
        svc.events.return_value.get = get_mock

        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal")
        result = cmd_get_notes(svc, "evt123")

        assert "Error" in result


# ===========================================================================
# cmd_set_notes
# ===========================================================================


class TestCmdSetNotes:
    def test_patches_only_description(self, monkeypatch):
        patch_calls: list[dict] = []

        def fake_patch(calendarId, eventId, body):
            patch_calls.append({"calendarId": calendarId, "eventId": eventId, "body": body})
            return MagicMock(execute=MagicMock(return_value={}))

        svc = MagicMock()
        svc.events.return_value.patch = fake_patch

        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal")
        result = cmd_set_notes(svc, "evt123", "new text")

        assert result.startswith("✓")
        assert len(patch_calls) == 1
        body = patch_calls[0]["body"]
        # Only description must be in the patch body — no other fields
        assert list(body.keys()) == ["description"]
        assert body["description"] == "new text"

    def test_no_home_calendar_returns_error(self, monkeypatch):
        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "")
        svc = MagicMock()

        result = cmd_set_notes(svc, "evt123", "text")

        assert "Error" in result

    def test_api_error_returns_error_string(self, monkeypatch):
        patch_mock = MagicMock(
            return_value=MagicMock(execute=MagicMock(side_effect=Exception("500")))
        )
        svc = MagicMock()
        svc.events.return_value.patch = patch_mock

        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal")
        result = cmd_set_notes(svc, "evt123", "text")

        assert "Error" in result

    def test_set_notes_empty_string_clears_note(self, monkeypatch):
        patch_calls: list[dict] = []

        def fake_patch(calendarId, eventId, body):
            patch_calls.append(body)
            return MagicMock(execute=MagicMock(return_value={}))

        svc = MagicMock()
        svc.events.return_value.patch = fake_patch

        monkeypatch.setattr(_cal, "HOME_CALENDAR_ID", "home_cal")
        result = cmd_set_notes(svc, "evt123", "")

        assert result.startswith("✓")
        assert patch_calls[0]["description"] == ""
