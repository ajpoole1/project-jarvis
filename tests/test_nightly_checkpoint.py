"""Tests for 2026-0044: nightly checkpoint Haiku JSON parse robustness."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load scripts/nightly_checkpoint.py without running main()
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "nightly_checkpoint.py"
_spec = importlib.util.spec_from_file_location("nightly_checkpoint", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------------
# _extract_json_array — parse robustness
# ---------------------------------------------------------------------------


def test_extract_bare_array():
    raw = '[{"title": "t", "summary": "s", "suggested_path": "p", "confidence": 0.9}]'
    result = _mod._extract_json_array(raw)
    assert isinstance(result, list)
    assert result[0]["title"] == "t"


def test_extract_fenced_json():
    raw = '```json\n[{"title": "t", "summary": "s", "suggested_path": "p", "confidence": 0.9}]\n```'
    result = _mod._extract_json_array(raw)
    assert len(result) == 1
    assert result[0]["confidence"] == 0.9


def test_extract_fenced_no_lang():
    raw = '```\n[{"title": "x", "summary": "y", "suggested_path": "z", "confidence": 0.7}]\n```'
    result = _mod._extract_json_array(raw)
    assert result[0]["title"] == "x"


def test_extract_preamble_prose():
    raw = 'Sure, here is the array:\n[{"title": "a", "summary": "b", "suggested_path": "c", "confidence": 0.8}]'
    result = _mod._extract_json_array(raw)
    assert result[0]["title"] == "a"


def test_extract_empty_array():
    result = _mod._extract_json_array("[]")
    assert result == []


def test_extract_empty_string_raises():
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _mod._extract_json_array("")


def test_extract_no_array_raises():
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _mod._extract_json_array("Nothing here, just prose.")


def test_extract_unbalanced_raises():
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _mod._extract_json_array("[{oops")


# ---------------------------------------------------------------------------
# _run_haiku_sweep — retry on parse failure, strict system prompt on retry
# ---------------------------------------------------------------------------

_VALID_ARTIFACTS = [{"title": "T", "summary": "S", "suggested_path": "P", "confidence": 0.9}]
_FENCED_RESPONSE = f"```json\n{json.dumps(_VALID_ARTIFACTS)}\n```"
_BARE_RESPONSE = json.dumps(_VALID_ARTIFACTS)


def _make_haiku_response(text: str) -> dict:
    return {"content": [{"text": text}]}


def test_run_haiku_sweep_bare_json_succeeds():
    """Standard bare-JSON response parses on first attempt."""
    with patch.object(_mod, "_call_haiku", return_value=_BARE_RESPONSE) as mock_call:
        result = _mod._run_haiku_sweep("some transcript")
    assert result == _VALID_ARTIFACTS
    assert mock_call.call_count == 1
    # First call should NOT use strict mode
    assert (
        mock_call.call_args_list[0][1].get("strict") is False
        or mock_call.call_args_list[0][0][1] is False
    )


def test_run_haiku_sweep_fenced_json_succeeds_first_attempt():
    """Markdown-fenced JSON parses on first attempt without retry."""
    with patch.object(_mod, "_call_haiku", return_value=_FENCED_RESPONSE) as mock_call:
        result = _mod._run_haiku_sweep("some transcript")
    assert result == _VALID_ARTIFACTS
    assert mock_call.call_count == 1


def test_run_haiku_sweep_retries_on_parse_failure():
    """Garbage on attempt 1 → retry with strict=True → success on attempt 2."""
    with patch.object(
        _mod, "_call_haiku", side_effect=["not json at all", _BARE_RESPONSE]
    ) as mock_call:
        result = _mod._run_haiku_sweep("transcript")
    assert result == _VALID_ARTIFACTS
    assert mock_call.call_count == 2
    # Second call must use strict=True
    _, kwargs = mock_call.call_args_list[1]
    assert kwargs.get("strict") is True or mock_call.call_args_list[1][0][1] is True


def test_run_haiku_sweep_raises_after_two_failures():
    """Two consecutive parse failures raise ValueError."""
    with patch.object(_mod, "_call_haiku", return_value="still not json"):
        with pytest.raises(ValueError, match="unparseable after 2 attempts"):
            _mod._run_haiku_sweep("transcript")


def test_run_haiku_sweep_empty_output_retries():
    """Empty string on attempt 1 triggers retry."""
    with patch.object(_mod, "_call_haiku", side_effect=["", _BARE_RESPONSE]) as mock_call:
        result = _mod._run_haiku_sweep("transcript")
    assert result == _VALID_ARTIFACTS
    assert mock_call.call_count == 2


# ---------------------------------------------------------------------------
# phase_capture — parse failure proceeds to restart via raw fallback
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "jarvis.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE knowledge_pending_writes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            suggested_path TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK(provenance IN ("explicit-capture","compaction-rescue","nightly-checkpoint","nightly-checkpoint-raw")),
            confidence REAL NOT NULL DEFAULT 1.0,
            filed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    return db


def test_phase_capture_parse_failure_writes_raw_fallback_and_returns_true(tmp_path):
    """
    When Haiku output is unparseable, phase_capture writes a raw fallback row
    and returns (True, ...) so the restart is NOT skipped.
    """
    db = _make_db(tmp_path)
    transcript = "USER: some content\nASSISTANT: some reply"

    with (
        patch.object(_mod, "_read_session_transcript", return_value=transcript),
        patch.object(
            _mod, "_run_haiku_sweep", side_effect=ValueError("unparseable after 2 attempts")
        ),
        patch.object(_mod, "_alert"),
        patch.object(_mod, "_update_live_state"),
        patch.object(_mod, "DB_PATH", db),
    ):
        ok, desc = _mod.phase_capture()

    assert ok is True, f"Expected True (restart should proceed), got False: {desc}"
    assert "raw fallback" in desc.lower() or "proceeding" in desc.lower()

    # Confirm the raw row was written
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT title, provenance, summary FROM knowledge_pending_writes"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    title, provenance, summary = rows[0]
    assert provenance == "nightly-checkpoint-raw"
    assert "[RAW]" in title
    assert summary == transcript


def test_phase_capture_parse_failure_posts_alert(tmp_path):
    """Alert is posted when falling back to raw, mentioning manual review."""
    db = _make_db(tmp_path)

    with (
        patch.object(_mod, "_read_session_transcript", return_value="some text"),
        patch.object(_mod, "_run_haiku_sweep", side_effect=ValueError("bad output")),
        patch.object(_mod, "_alert") as mock_alert,
        patch.object(_mod, "_update_live_state"),
        patch.object(_mod, "DB_PATH", db),
    ):
        _mod.phase_capture()

    assert mock_alert.call_count == 1
    alert_msg = mock_alert.call_args[0][0]
    assert "nightly-checkpoint-raw" in alert_msg or "knowledge pending" in alert_msg.lower()


def test_phase_capture_raw_fallback_db_failure_skips_restart(tmp_path):
    """
    If the raw fallback DB write also fails, return (False, ...) to skip restart
    — transcript is genuinely at risk of loss.
    """
    with (
        patch.object(_mod, "_read_session_transcript", return_value="some text"),
        patch.object(_mod, "_run_haiku_sweep", side_effect=ValueError("bad")),
        patch.object(_mod, "_stage_raw_fallback", side_effect=Exception("db down")),
        patch.object(_mod, "_alert"),
        patch.object(_mod, "_update_live_state"),
    ):
        ok, desc = _mod.phase_capture()

    assert ok is False
    assert "raw fallback failed" in desc


def test_phase_capture_clean_sweep_still_returns_true(tmp_path):
    """Normal path: sweep succeeds → (True, 'captured N item(s)')."""
    db = _make_db(tmp_path)
    artifacts = [{"title": "T", "summary": "S", "suggested_path": "P", "confidence": 0.9}]

    with (
        patch.object(_mod, "_read_session_transcript", return_value="transcript"),
        patch.object(_mod, "_run_haiku_sweep", return_value=artifacts),
        patch.object(_mod, "_update_live_state"),
        patch.object(_mod, "_info"),
        patch.object(_mod, "DB_PATH", db),
    ):
        ok, desc = _mod.phase_capture()

    assert ok is True
    assert "captured" in desc
