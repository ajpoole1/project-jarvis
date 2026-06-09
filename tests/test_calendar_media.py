"""Tests for calendar pagination (Part 1) and watched-media store (Part 2)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load modules without running main()
# ---------------------------------------------------------------------------

_CALENDAR_PATH = Path(__file__).parents[1] / "skills" / "calendar" / "skill.py"
_MEDIA_PATH = Path(__file__).parents[1] / "skills" / "media" / "skill.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Prevent Google API imports from failing in unit tests
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
    spec.loader.exec_module(mod)
    return mod


_cal = _load_module(_CALENDAR_PATH, "calendar_skill")
_media = _load_module(_MEDIA_PATH, "media_skill")

_paginate_events = _cal._paginate_events
_PAGINATION_SAFETY_CAP = _cal._PAGINATION_SAFETY_CAP

_normalize = _media._normalize
_SEED = _media._SEED


# ===========================================================================
# Part 1 — Calendar pagination
# ===========================================================================


def _make_service(*pages: list[dict], extra_page_token: bool = False) -> MagicMock:
    """Build a mock service whose events().list().execute() returns pages in order."""
    responses = []
    for i, items in enumerate(pages):
        is_last = i == len(pages) - 1
        resp = {"items": items}
        if not is_last or extra_page_token:
            resp["nextPageToken"] = f"tok{i + 1}"
        responses.append(resp)

    execute_mock = MagicMock(side_effect=responses)
    list_mock = MagicMock(return_value=MagicMock(execute=execute_mock))
    events_mock = MagicMock(return_value=MagicMock(list=list_mock))
    service = MagicMock()
    service.events = events_mock
    return service


def _make_event(summary: str, start_dt: str) -> dict:
    return {"summary": summary, "start": {"dateTime": start_dt}, "end": {"dateTime": start_dt}}


class TestPaginateEvents:
    def test_single_page_no_truncation(self):
        items = [_make_event(f"ev{i}", f"2026-06-01T0{i}:00:00") for i in range(3)]
        svc = _make_service(items)

        events, truncated = _paginate_events(svc, "primary")

        assert len(events) == 3
        assert truncated is False

    def test_multi_page_returns_all(self):
        page1 = [_make_event(f"ev{i}", f"2026-06-01T0{i}:00:00") for i in range(5)]
        page2 = [_make_event(f"ev{i}", f"2026-06-01T0{i}:00:00") for i in range(5, 9)]
        svc = _make_service(page1, page2)

        events, truncated = _paginate_events(svc, "primary")

        assert len(events) == 9
        assert truncated is False

    def test_more_than_50_events_returned(self):
        """Regression: the old maxResults=50 cap would silently drop events."""
        pages = []
        total = 60
        for batch_start in range(0, total, 10):
            pages.append(
                [
                    _make_event(f"ev{i}", f"2026-06-{i + 1:02d}T09:00:00")
                    for i in range(batch_start, min(batch_start + 10, total))
                ]
            )
        svc = _make_service(*pages)

        events, truncated = _paginate_events(svc, "primary")

        assert len(events) == total
        assert truncated is False

    def test_safety_cap_triggers_truncation(self):
        """When safety_cap is hit with a nextPageToken pending, truncated=True."""
        page1 = [_make_event(f"ev{i}", "2026-06-01T00:00:00") for i in range(5)]
        # extra_page_token=True simulates more pages beyond what we've fetched
        svc = _make_service(page1, extra_page_token=True)

        events, truncated = _paginate_events(svc, "primary", safety_cap=5)

        assert len(events) == 5
        assert truncated is True

    def test_no_truncation_warning_on_natural_end(self):
        """A fully-paginated result must not set truncated=True."""
        page1 = [_make_event(f"ev{i}", "2026-06-01T00:00:00") for i in range(3)]
        svc = _make_service(page1)

        _, truncated = _paginate_events(svc, "primary", safety_cap=3)

        assert truncated is False

    def test_truncation_warning_logged_to_stderr(self, capsys):
        """fetch_events logs ⚠️ to stderr when the safety cap is hit."""
        page1 = [_make_event(f"ev{i}", f"2026-06-{i + 1:02d}T09:00:00") for i in range(3)]
        svc = _make_service(page1, extra_page_token=True)

        with patch.object(_cal, "CALENDAR_IDS", ["primary"]):
            with patch.object(_cal, "_paginate_events", return_value=(page1, True)):
                _cal.fetch_events(svc, "2026-06-01T00:00:00", "2026-06-30T23:59:59")

        captured = capsys.readouterr()
        assert "⚠️" in captured.err
        assert "safety cap" in captured.err

    def test_no_warning_on_clean_fetch(self, capsys):
        """fetch_events must NOT emit any warning when results are complete."""
        page1 = [_make_event(f"ev{i}", f"2026-06-{i + 1:02d}T09:00:00") for i in range(3)]
        svc = _make_service(page1)

        with patch.object(_cal, "CALENDAR_IDS", ["primary"]):
            with patch.object(_cal, "_paginate_events", return_value=(page1, False)):
                _cal.fetch_events(svc, "2026-06-01T00:00:00", "2026-06-30T23:59:59")

        captured = capsys.readouterr()
        assert "⚠️" not in captured.err

    def test_page_token_passed_on_subsequent_calls(self):
        """Each page after the first must forward the nextPageToken."""
        page1 = [_make_event("ev0", "2026-06-01T09:00:00")]
        page2 = [_make_event("ev1", "2026-06-02T09:00:00")]
        svc = _make_service(page1, page2)

        _paginate_events(svc, "primary", timeMin="x", timeMax="y")

        list_mock = svc.events().list
        calls = list_mock.call_args_list
        assert len(calls) == 2
        # Second call must carry pageToken — collect from positional or keyword args
        all_args = {**calls[1][1]} if calls[1][1] else {}
        all_args.update(calls[1].kwargs or {})
        assert "pageToken" in all_args


# ===========================================================================
# Part 2 — Watched-media store
# ===========================================================================


@pytest.fixture()
def media_db(tmp_path, monkeypatch):
    """Provide an isolated media skill with a temp DB."""
    monkeypatch.setattr(_media, "_DB_PATH", tmp_path / "jarvis.db")
    return tmp_path


class TestNormalize:
    def test_lowercase(self):
        assert _normalize("SEVERANCE") == "severance"

    def test_strip_leading_the(self):
        assert _normalize("The Summer I Turned Pretty") == "summer i turned pretty"

    def test_strip_leading_a(self):
        assert _normalize("A Beautiful Mind") == "beautiful mind"

    def test_strip_leading_an(self):
        assert _normalize("An Education") == "education"

    def test_strip_punctuation(self):
        assert _normalize("It's Always Sunny!") == "its always sunny"

    def test_collapse_whitespace(self):
        assert _normalize("  Normal   People  ") == "normal people"

    def test_article_not_stripped_mid_title(self):
        assert _normalize("Fear the Walking Dead") == "fear the walking dead"

    def test_case_variants_match(self):
        assert _normalize("the summer i turned pretty") == _normalize("The Summer I Turned Pretty")
        assert _normalize("SUMMER I TURNED PRETTY") == _normalize("summer i turned pretty")


class TestSeed:
    def test_seed_entries_exist_after_init(self, media_db):
        conn = _media._init_db()
        rows = conn.execute("SELECT person, title_norm FROM media_log ORDER BY id").fetchall()
        expected_norms = {(_normalize(t), p) for p, t, *_ in _SEED}
        actual = {(norm, person) for person, norm in rows}
        assert actual == expected_norms

    def test_seed_is_idempotent(self, media_db):
        _media._init_db()
        _media._init_db()
        _media._init_db()
        conn = _media._init_db()
        count = conn.execute("SELECT COUNT(*) FROM media_log").fetchone()[0]
        assert count == len(_SEED), "re-running init must not duplicate seed entries"


class TestCmdSeen:
    def test_seen_exact_match(self, media_db):
        result = _media.cmd_seen(["The Summer I Turned Pretty"])
        assert "Poli" in result
        assert "loved" in result

    def test_seen_lowercase_no_article(self, media_db):
        result = _media.cmd_seen(["the summer i turned pretty"])
        assert "Poli" in result

    def test_seen_title_case_no_article(self, media_db):
        result = _media.cmd_seen(["Summer I Turned Pretty"])
        assert "Poli" in result

    def test_not_seen_returns_not_seen(self, media_db):
        result = _media.cmd_seen(["Severance"])
        assert "Not seen" in result


class TestCmdAdd:
    def test_add_new_entry(self, media_db):
        result = _media.cmd_add(["AJ", "Severance", "loved"])
        assert "Logged" in result
        assert "Severance" in result

    def test_add_duplicate_blocked(self, media_db):
        _media.cmd_add(["AJ", "Severance", "loved"])
        result = _media.cmd_add(["AJ", "Severance", "ok"])
        assert "Already logged" in result

    def test_add_invalid_verdict(self, media_db):
        result = _media.cmd_add(["AJ", "Severance", "meh"])
        assert "Invalid verdict" in result

    def test_add_with_kind_and_note(self, media_db):
        result = _media.cmd_add(["AJ", "Dune", "loved", "--kind", "movie", "--note", "epic"])
        assert "Logged" in result
        conn = _media._init_db()
        row = conn.execute(
            "SELECT kind, note FROM media_log WHERE title_norm = ?", (_normalize("Dune"),)
        ).fetchone()
        assert row[0] == "movie"
        assert row[1] == "epic"

    def test_add_invalid_kind(self, media_db):
        result = _media.cmd_add(["AJ", "Minecraft", "loved", "--kind", "podcast"])
        assert "Invalid kind" in result


class TestCmdList:
    def test_list_all(self, media_db):
        result = _media.cmd_list([])
        assert "AJ" in result
        assert "Poli" in result

    def test_list_filtered_by_person(self, media_db):
        result = _media.cmd_list(["AJ"])
        assert "AJ" in result
        assert "Poli" not in result

    def test_list_empty_person(self, media_db, monkeypatch):
        monkeypatch.setattr(_media, "_SEED", [])
        monkeypatch.setattr(_media, "_DB_PATH", media_db / "empty.db")
        _media._init_db()
        result = _media.cmd_list(["nobody"])
        assert "No media entries" in result


class TestCmdFilter:
    def test_filter_splits_fresh_vs_seen(self, media_db):
        raw = _media.cmd_filter(["--titles", "Severance,The Summer I Turned Pretty,Normal People"])
        data = json.loads(raw)
        assert "Severance" in data["fresh"]
        seen_titles = [t.lower() for t in data["seen"]]
        assert any("summer" in t for t in seen_titles)

    def test_filter_no_input_returns_empty(self, media_db):
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        mock_stdin.__iter__ = MagicMock(return_value=iter([]))
        with patch("sys.stdin", mock_stdin):
            raw = _media.cmd_filter([])
        data = json.loads(raw)
        assert data == {"fresh": [], "seen": []}

    def test_filter_seen_normalized_match(self, media_db):
        raw = _media.cmd_filter(["--titles", "summer i turned pretty"])
        data = json.loads(raw)
        assert data["seen"]
        assert not any(t == "summer i turned pretty" for t in data["fresh"])

    def test_filter_all_fresh(self, media_db):
        raw = _media.cmd_filter(["--titles", "Severance,Slow Horses"])
        data = json.loads(raw)
        assert len(data["fresh"]) == 2
        assert data["seen"] == []
