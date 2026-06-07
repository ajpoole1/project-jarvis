"""Tests for the once@ one-off schedule format and retirement logic."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# ---------------------------------------------------------------------------
# Import skills/schedules/skill.py without running main()
# ---------------------------------------------------------------------------

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "schedules" / "skill.py"
_spec = importlib.util.spec_from_file_location("schedules_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_compute_next_run = _mod._compute_next_run
_is_oneoff = _mod._is_oneoff
_record_run = _mod._record_run
_LOCAL_TZ = ZoneInfo("America/Toronto")


# ---------------------------------------------------------------------------
# _is_oneoff
# ---------------------------------------------------------------------------


def test_is_oneoff_true():
    assert _is_oneoff("once@2026-06-10T14:30") is True


def test_is_oneoff_case_insensitive():
    assert _is_oneoff("Once@2026-06-10T14:30") is True


def test_is_oneoff_false_recurring():
    assert _is_oneoff("daily@09:00") is False
    assert _is_oneoff("30m") is False
    assert _is_oneoff("weekly@mon@09:00") is False


# ---------------------------------------------------------------------------
# _compute_next_run — once@ branch
# ---------------------------------------------------------------------------


def test_compute_once_returns_correct_utc():
    # 2026-06-10T14:30 America/Toronto = EDT (UTC-4) = 18:30 UTC
    result = _compute_next_run("once@2026-06-10T14:30", datetime.now(UTC))
    expected = datetime(2026, 6, 10, 18, 30, tzinfo=UTC)
    assert result == expected


def test_compute_once_ignores_from_dt():
    # The fixed instant should be the same regardless of from_dt
    from_past = datetime(2020, 1, 1, tzinfo=UTC)
    from_future = datetime(2030, 1, 1, tzinfo=UTC)
    r1 = _compute_next_run("once@2026-06-10T14:30", from_past)
    r2 = _compute_next_run("once@2026-06-10T14:30", from_future)
    assert r1 == r2


def test_compute_once_winter_utc_offset():
    # 2026-01-15T10:00 America/Toronto = EST (UTC-5) = 15:00 UTC
    result = _compute_next_run("once@2026-01-15T10:00", datetime.now(UTC))
    expected = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
    assert result == expected


def test_compute_once_malformed_raises():
    with pytest.raises(ValueError, match="Malformed once@"):
        _compute_next_run("once@not-a-date", datetime.now(UTC))


def test_compute_once_malformed_missing_time_raises():
    with pytest.raises(ValueError, match="Malformed once@"):
        _compute_next_run("once@2026-06-10", datetime.now(UTC))


def test_compute_unsupported_format_raises():
    with pytest.raises(ValueError, match="Unsupported schedule format"):
        _compute_next_run("bogus@format", datetime.now(UTC))


# ---------------------------------------------------------------------------
# _record_run — one-off retirement
# ---------------------------------------------------------------------------


def _make_test_conn() -> sqlite3.Connection:
    """Return an in-memory DB with the schedules table pre-created."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE schedules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            skill       TEXT NOT NULL,
            args        TEXT NOT NULL DEFAULT '[]',
            schedule    TEXT NOT NULL,
            next_run    TEXT NOT NULL,
            last_run    TEXT,
            last_status TEXT,
            enabled     INTEGER NOT NULL DEFAULT 1,
            approved    INTEGER NOT NULL DEFAULT 0,
            created_by  TEXT NOT NULL DEFAULT 'jarvis',
            description TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()
    return conn


def _insert_schedule(conn: sqlite3.Connection, schedule: str, enabled: int = 1) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """INSERT INTO schedules (created_at, skill, args, schedule, next_run, enabled, approved)
           VALUES (?, 'reminder', '[]', ?, ?, ?, 1)""",
        (now, schedule, now, enabled),
    )
    conn.commit()
    return cur.lastrowid


def test_record_run_oneoff_sets_enabled_zero():
    conn = _make_test_conn()
    job_id = _insert_schedule(conn, "once@2026-06-10T14:30")
    ran_at = datetime.now(UTC)

    _record_run(conn, job_id, "once@2026-06-10T14:30", ran_at, "ok")

    row = conn.execute(
        "SELECT enabled, last_status FROM schedules WHERE id = ?", (job_id,)
    ).fetchone()
    assert row[0] == 0, "one-off should be disabled after firing"
    assert "fired, one-off retired" in row[1]


def test_record_run_oneoff_next_run_unchanged():
    conn = _make_test_conn()
    original_next_run = "2026-06-10T18:30:00+00:00"
    conn.execute(
        """INSERT INTO schedules (created_at, skill, args, schedule, next_run, enabled, approved)
           VALUES (?, 'reminder', '[]', ?, ?, 1, 1)""",
        (datetime.now(UTC).isoformat(), "once@2026-06-10T14:30", original_next_run),
    )
    conn.commit()
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    ran_at = datetime.now(UTC)
    _record_run(conn, job_id, "once@2026-06-10T14:30", ran_at, "ok")

    row = conn.execute("SELECT next_run FROM schedules WHERE id = ?", (job_id,)).fetchone()
    assert row[0] == original_next_run, "next_run must not be updated for a one-off"


def test_record_run_recurring_still_reschedules():
    conn = _make_test_conn()
    job_id = _insert_schedule(conn, "daily@09:00")
    ran_at = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)  # 09:00 EDT

    _record_run(conn, job_id, "daily@09:00", ran_at, "ok")

    row = conn.execute(
        "SELECT enabled, last_status, next_run FROM schedules WHERE id = ?", (job_id,)
    ).fetchone()
    assert row[0] == 1, "recurring job must stay enabled"
    assert "fired, one-off retired" not in (row[1] or "")
    # next_run should be different from ran_at (rescheduled for next day)
    next_run_dt = datetime.fromisoformat(row[2])
    assert next_run_dt > ran_at
