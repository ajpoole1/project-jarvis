"""Tests for followups periodic re-arm anchor fix (#45).

A periodic follow-up fired late must re-arm to its *original* weekday/time,
not to late_fire + cadence_days.  Specifically, _mark_surfaced must advance
from the intended trigger_at by cadence increments until the result is in
the future — never from _now_utc().
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "followups" / "skill.py"
_spec = importlib.util.spec_from_file_location("followups_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE follow_ups (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at       TEXT NOT NULL,
            subject          TEXT NOT NULL,
            prompt           TEXT NOT NULL,
            policy           TEXT NOT NULL DEFAULT 'check_once',
            trigger_at       TEXT NOT NULL,
            window_until     TEXT,
            cadence          INTEGER,
            priority         INTEGER NOT NULL DEFAULT 5,
            status           TEXT NOT NULL DEFAULT 'pending',
            surface_count    INTEGER NOT NULL DEFAULT 0,
            last_surfaced_at TEXT,
            resolved_at      TEXT,
            resolution       TEXT,
            knowledge_target TEXT,
            source           TEXT NOT NULL DEFAULT 'explicit',
            thread_id        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE jarvis_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)
    """)
    conn.commit()
    return conn


def _insert_periodic(
    conn: sqlite3.Connection,
    trigger_at: datetime,
    cadence: int = 7,
) -> int:
    cur = conn.execute(
        """INSERT INTO follow_ups
           (created_at, subject, prompt, policy, trigger_at, cadence, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(UTC).isoformat(),
            "weekly check-in",
            "How's the project?",
            "periodic",
            trigger_at.isoformat(),
            cadence,
            "pending",
        ),
    )
    conn.commit()
    return cur.lastrowid


def _get_trigger_at(conn: sqlite3.Connection, row_id: int) -> datetime:
    row = conn.execute("SELECT trigger_at FROM follow_ups WHERE id = ?", (row_id,)).fetchone()
    dt = datetime.fromisoformat(row[0])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_periodic_rearms_to_next_cadence_from_intended_trigger():
    """On-time fire: next trigger = original trigger + cadence_days."""
    conn = _make_db()
    now = datetime.now(UTC)
    # Intended trigger: one hour ago (just fired on time).
    intended = now - timedelta(hours=1)
    row_id = _insert_periodic(conn, intended, cadence=7)

    _mod._mark_surfaced(conn, row_id, "periodic", 7, intended.isoformat())

    next_trigger = _get_trigger_at(conn, row_id)
    expected = intended + timedelta(days=7)
    # Allow a 5-second tolerance for test execution time.
    assert abs((next_trigger - expected).total_seconds()) < 5


def test_periodic_late_fire_anchors_to_intended_weekday():
    """Late fire: next trigger still advances from the INTENDED trigger, not from now.

    Scenario: weekly Monday intention fires on Tuesday (1 day late).
    Expected next trigger = intended_Monday + 7 days = next Monday.
    Actual (buggy): now_Tuesday + 7 days = next Tuesday — wrong weekday.
    """
    conn = _make_db()
    now = datetime.now(UTC)
    # Intended: last Monday (6 days ago if today is Sunday, or simulate by subtracting).
    # We test the invariant directly: intended 6 days ago, cadence 7.
    intended = now - timedelta(days=6)  # should have fired 6 days ago
    row_id = _insert_periodic(conn, intended, cadence=7)

    _mod._mark_surfaced(conn, row_id, "periodic", 7, intended.isoformat())

    next_trigger = _get_trigger_at(conn, row_id)

    # Next trigger must be in the future.
    assert next_trigger > now

    # Next trigger must be anchored: intended + 7 days (since intended was 6 days ago,
    # intended+7 is 1 day in the future — correct).
    expected = intended + timedelta(days=7)
    assert abs((next_trigger - expected).total_seconds()) < 5

    # IMPORTANT: the buggy path would give now + 7 days (7 days in future),
    # which is 6 days later than expected. Verify we're NOT doing that.
    buggy_result = now + timedelta(days=7)
    assert abs((next_trigger - buggy_result).total_seconds()) > 60 * 60 * 24 * 5


def test_periodic_missed_multiple_cadences_advances_past_now():
    """Missed several cadences: must still land in the future, not in the past."""
    conn = _make_db()
    now = datetime.now(UTC)
    # Intended 21 days ago, cadence 7: would have needed 3 fires to catch up.
    intended = now - timedelta(days=21)
    row_id = _insert_periodic(conn, intended, cadence=7)

    _mod._mark_surfaced(conn, row_id, "periodic", 7, intended.isoformat())

    next_trigger = _get_trigger_at(conn, row_id)
    assert next_trigger > now

    # Must be exactly 28 days from intended (intended + 4*7), the first future slot.
    expected = intended + timedelta(days=28)
    assert abs((next_trigger - expected).total_seconds()) < 5


def test_periodic_no_trigger_at_falls_back_to_now_plus_cadence():
    """When trigger_at_iso is None (legacy rows), fall back to now+cadence."""
    conn = _make_db()
    now = datetime.now(UTC)
    intended = now - timedelta(hours=1)
    row_id = _insert_periodic(conn, intended, cadence=7)

    _mod._mark_surfaced(conn, row_id, "periodic", 7, None)

    next_trigger = _get_trigger_at(conn, row_id)
    expected = now + timedelta(days=7)
    # Allow 5 seconds for test execution time.
    assert abs((next_trigger - expected).total_seconds()) < 5


def test_persistent_rearms_with_anchor():
    """Persistent policy also benefits from anchor-based re-arm."""
    conn = _make_db()
    now = datetime.now(UTC)
    intended = now - timedelta(days=2)  # late by 2 days; cadence=3 → next = intended+3=1 day future
    row_id = _insert_periodic(conn, intended, cadence=3)
    conn.execute("UPDATE follow_ups SET policy='persistent' WHERE id=?", (row_id,))
    conn.commit()

    _mod._mark_surfaced(conn, row_id, "persistent", 3, intended.isoformat())

    next_trigger = _get_trigger_at(conn, row_id)
    assert next_trigger > now
    expected = intended + timedelta(days=3)
    assert abs((next_trigger - expected).total_seconds()) < 5
