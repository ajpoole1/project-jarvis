"""Tests for price-monitor per-watch deal_drop_pct threshold (2026-0047).

Covers:
  T1  _detect_deal uses per-watch threshold when set
  T2  _detect_deal falls back to global when watch threshold is None
  T3  Per-watch threshold of 10% triggers where global 15% would not
  T4  Global threshold still applies to watches with NULL deal_drop_pct
  T5  cmd_set updates deal_drop_pct on an existing watch
  T6  cmd_set --deal-drop-pct reset clears the override (NULL)
  T7  ALTER TABLE migration: existing rows stay NULL (fall back to global)
  T8  cmd_add stores deal_drop_pct when --deal-drop-pct is passed
  T9  cmd_list includes Drop% column
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "price-monitor" / "skill.py"


class _NoCloseConn:
    """Wraps sqlite3.Connection and suppresses close() so tests can inspect after cmd_ calls."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        pass  # suppress

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


_MOD_NAME = "price_monitor_skill_threshold"
if _MOD_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _SKILL_PATH)
    skill = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = skill
    _spec.loader.exec_module(skill)
else:
    skill = sys.modules[_MOD_NAME]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """In-memory DB with price_watches and price_history tables."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE price_watches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            url           TEXT NOT NULL,
            vendor        TEXT,
            target_price  REAL,
            currency      TEXT,
            last_price    REAL,
            last_checked  TEXT,
            parse_method  TEXT,
            status        TEXT NOT NULL DEFAULT 'active',
            fail_count    INTEGER NOT NULL DEFAULT 0,
            active_from   TEXT,
            active_until  TEXT,
            created_at    TEXT NOT NULL DEFAULT '2026-01-01T00:00:00',
            deal_drop_pct REAL
        )
    """)
    conn.execute("""
        CREATE TABLE price_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id   INTEGER NOT NULL,
            price      REAL NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _seed_watch(conn: sqlite3.Connection, deal_drop_pct=None, name="Test Item") -> int:
    cur = conn.execute(
        """INSERT INTO price_watches (name, url, status, created_at, deal_drop_pct)
           VALUES (?, 'https://example.com', 'active', '2026-01-01T00:00:00', ?)""",
        (name, deal_drop_pct),
    )
    conn.commit()
    return cur.lastrowid


def _seed_history(conn: sqlite3.Connection, watch_id: int, prices: list[float]) -> None:
    """Insert price history rows with incrementing timestamps so ordering is stable."""
    for i, p in enumerate(prices):
        conn.execute(
            "INSERT INTO price_history (watch_id, price, checked_at) VALUES (?, ?, ?)",
            (watch_id, p, f"2026-01-{i + 1:02d}T12:00:00"),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# T1 — per-watch threshold used when set
# ---------------------------------------------------------------------------


def test_detect_deal_uses_per_watch_threshold():
    """_detect_deal must use the watch's deal_drop_pct when provided."""
    conn = _make_db()
    watch_id = _seed_watch(conn)
    # 5 prior readings at $100 → median = $100
    _seed_history(conn, watch_id, [100.0] * 5)
    inserted_at = "2026-01-10T12:00:00"
    conn.execute(
        "INSERT INTO price_history (watch_id, price, checked_at) VALUES (?, ?, ?)",
        (watch_id, 88.0, inserted_at),
    )
    conn.commit()

    # 12% drop; per-watch threshold = 10% → should trigger
    result = skill._detect_deal(conn, watch_id, 88.0, None, inserted_at, deal_drop_pct=10.0)
    assert result is not None, "Expected deal with 12% drop and 10% threshold"
    assert "12%" in result or "median" in result


# ---------------------------------------------------------------------------
# T2 — falls back to global when watch threshold is None
# ---------------------------------------------------------------------------


def test_detect_deal_falls_back_to_global_when_none():
    """_detect_deal must use DEAL_DROP_PCT when deal_drop_pct=None."""
    conn = _make_db()
    watch_id = _seed_watch(conn)
    _seed_history(conn, watch_id, [100.0] * 5)
    inserted_at = "2026-01-10T12:00:00"
    conn.execute(
        "INSERT INTO price_history (watch_id, price, checked_at) VALUES (?, ?, ?)",
        (watch_id, 88.0, inserted_at),
    )
    conn.commit()

    global_pct = skill.DEAL_DROP_PCT
    # 12% drop; pass None → falls back to global
    result = skill._detect_deal(conn, watch_id, 88.0, None, inserted_at, deal_drop_pct=None)
    if global_pct <= 12.0:
        assert result is not None, f"Expected deal: 12% drop >= global {global_pct}%"
    else:
        assert result is None, f"No deal expected: 12% drop < global {global_pct}%"


# ---------------------------------------------------------------------------
# T3 — per-watch 10% triggers where global 15% would not
# ---------------------------------------------------------------------------


def test_per_watch_threshold_triggers_where_global_would_not():
    """A 12% drop should trigger with threshold=10 but not with threshold=15."""
    conn = _make_db()
    watch_id = _seed_watch(conn)
    _seed_history(conn, watch_id, [100.0] * 5)
    inserted_at = "2026-01-10T12:00:00"
    conn.execute(
        "INSERT INTO price_history (watch_id, price, checked_at) VALUES (?, ?, ?)",
        (watch_id, 88.0, inserted_at),
    )
    conn.commit()

    result_strict = skill._detect_deal(conn, watch_id, 88.0, None, inserted_at, deal_drop_pct=15.0)
    result_loose = skill._detect_deal(conn, watch_id, 88.0, None, inserted_at, deal_drop_pct=10.0)

    assert result_strict is None, "15% threshold should not trigger on 12% drop"
    assert result_loose is not None, "10% threshold should trigger on 12% drop"


# ---------------------------------------------------------------------------
# T4 — global threshold applies to watches with NULL deal_drop_pct
# ---------------------------------------------------------------------------


def test_global_threshold_applies_when_watch_has_null():
    """Existing watches (deal_drop_pct=NULL) must use global DEAL_DROP_PCT."""
    conn = _make_db()
    watch_id = _seed_watch(conn, deal_drop_pct=None)
    _seed_history(conn, watch_id, [100.0] * 5)
    inserted_at = "2026-01-10T12:00:00"
    conn.execute(
        "INSERT INTO price_history (watch_id, price, checked_at) VALUES (?, ?, ?)",
        (watch_id, 80.0, inserted_at),
    )
    conn.commit()

    # 20% drop — should trigger regardless of global (default is 15%)
    result = skill._detect_deal(conn, watch_id, 80.0, None, inserted_at, deal_drop_pct=None)
    assert result is not None, "20% drop should trigger global threshold"


# ---------------------------------------------------------------------------
# T5 — cmd_set updates deal_drop_pct
# ---------------------------------------------------------------------------


def test_cmd_set_updates_deal_drop_pct(capsys):
    """cmd_set --id N --deal-drop-pct 10 must update the DB row."""
    raw_conn = _make_db()
    watch_id = _seed_watch(raw_conn, name="PC Build")
    conn = _NoCloseConn(raw_conn)

    with patch.object(skill, "_init_db", return_value=conn):
        skill.cmd_set(["--id", str(watch_id), "--deal-drop-pct", "10"])

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["deal_drop_pct"] == 10.0
    assert data["id"] == watch_id

    row = raw_conn.execute(
        "SELECT deal_drop_pct FROM price_watches WHERE id = ?", (watch_id,)
    ).fetchone()
    assert row[0] == 10.0


# ---------------------------------------------------------------------------
# T6 — cmd_set reset clears the override
# ---------------------------------------------------------------------------


def test_cmd_set_reset_clears_override(capsys):
    """cmd_set --deal-drop-pct reset must set deal_drop_pct back to NULL."""
    raw_conn = _make_db()
    watch_id = _seed_watch(raw_conn, deal_drop_pct=10.0, name="Drone")
    conn = _NoCloseConn(raw_conn)

    with patch.object(skill, "_init_db", return_value=conn):
        skill.cmd_set(["--id", str(watch_id), "--deal-drop-pct", "reset"])

    row = raw_conn.execute(
        "SELECT deal_drop_pct FROM price_watches WHERE id = ?", (watch_id,)
    ).fetchone()
    assert row[0] is None, "reset should clear the per-watch threshold to NULL"


# ---------------------------------------------------------------------------
# T7 — migration: existing rows stay NULL
# ---------------------------------------------------------------------------


def test_migration_existing_rows_stay_null():
    """After ALTER TABLE migration, pre-existing rows must have deal_drop_pct=NULL."""
    conn = sqlite3.connect(":memory:")
    # Create table WITHOUT deal_drop_pct (simulates pre-migration schema)
    conn.execute("""
        CREATE TABLE price_watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            fail_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00'
        )
    """)
    conn.execute(
        "INSERT INTO price_watches (name, url) VALUES ('Old Watch', 'https://example.com')"
    )
    conn.commit()
    # Simulate migration
    conn.execute("ALTER TABLE price_watches ADD COLUMN deal_drop_pct REAL")
    conn.commit()

    row = conn.execute(
        "SELECT deal_drop_pct FROM price_watches WHERE name = 'Old Watch'"
    ).fetchone()
    assert row[0] is None, "Existing rows must have NULL deal_drop_pct after migration"


# ---------------------------------------------------------------------------
# T8 — cmd_add stores deal_drop_pct
# ---------------------------------------------------------------------------


def test_cmd_add_stores_deal_drop_pct(capsys):
    """cmd_add --deal-drop-pct 10 must store the value in the DB."""
    conn = _make_db()

    def fake_probe(url):
        return 2600.0, "jsonld", "CAD"

    raw_conn = _make_db()
    conn = _NoCloseConn(raw_conn)
    with (
        patch.object(skill, "_init_db", return_value=conn),
        patch.object(skill, "_probe", side_effect=fake_probe),
    ):
        skill.cmd_add(
            [
                "--name",
                "High-End PC",
                "--url",
                "https://example.com/pc",
                "--deal-drop-pct",
                "10",
            ]
        )

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["deal_drop_pct"] == 10.0

    row = raw_conn.execute(
        "SELECT deal_drop_pct FROM price_watches WHERE name = 'High-End PC'"
    ).fetchone()
    assert row is not None
    assert row[0] == 10.0


# ---------------------------------------------------------------------------
# T9 — cmd_list includes Drop% column
# ---------------------------------------------------------------------------


def test_cmd_list_shows_drop_pct_column(capsys):
    """cmd_list output must include a Drop% column."""
    conn = _make_db()
    _seed_watch(conn, deal_drop_pct=10.0, name="Drone")
    _seed_watch(conn, deal_drop_pct=None, name="Cheap Widget")

    with patch.object(skill, "_init_db", return_value=conn):
        skill.cmd_list([])

    out = capsys.readouterr().out
    assert "Drop%" in out, "cmd_list must include Drop% header"
    assert "10%" in out, "Per-watch threshold should appear in list"
    # NULL watch should show the global default in parens
    assert f"({skill.DEAL_DROP_PCT:.0f}%)" in out, "NULL threshold should show global default"
