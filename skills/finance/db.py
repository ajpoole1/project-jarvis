"""Finance database layer — SQLite with finance.db schema.

Amount sign convention (enforced throughout):
  negative = money out (debit / spend / payment leaving bank)
  positive = money in  (income / deposit / CC payment received)

SUM(amount) < 0 means you spent more than came in.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    institution   TEXT NOT NULL,
    name          TEXT,
    type          TEXT,
    currency      TEXT DEFAULT 'CAD',
    balance_current REAL,
    owner         TEXT DEFAULT 'personal',
    source        TEXT DEFAULT 'wealthica',
    last_synced   TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id          TEXT PRIMARY KEY,
    account_id  TEXT REFERENCES accounts(id),
    date        TEXT NOT NULL,
    amount      REAL NOT NULL,
    description TEXT,
    category    TEXT,
    currency    TEXT DEFAULT 'CAD',
    owner       TEXT DEFAULT 'personal',
    is_pending  INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'wealthica',
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_txn_date       ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_account    ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_txn_owner      ON transactions(owner);
CREATE INDEX IF NOT EXISTS idx_txn_category   ON transactions(category);

CREATE TABLE IF NOT EXISTS recurring (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_norm   TEXT NOT NULL,
    amount_median   REAL,
    cadence_days    INTEGER,
    last_seen       TEXT,
    next_expected   TEXT,
    owner           TEXT DEFAULT 'personal',
    category        TEXT
);

CREATE TABLE IF NOT EXISTS rules (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern  TEXT NOT NULL,
    owner    TEXT NOT NULL,
    category TEXT,
    created  TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    key     TEXT PRIMARY KEY,
    value   TEXT,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    target_amount  REAL,
    current_amount REAL DEFAULT 0,
    account_id     TEXT REFERENCES accounts(id),
    notes          TEXT
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables if not exist. Returns open connection with row_factory."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------


def upsert_account(conn: sqlite3.Connection, account: dict) -> None:
    """Insert or update an account record."""
    conn.execute(
        """
        INSERT INTO accounts (id, institution, name, type, currency, balance_current, owner, source, last_synced)
        VALUES (:id, :institution, :name, :type, :currency, :balance_current, :owner, :source, :last_synced)
        ON CONFLICT(id) DO UPDATE SET
            institution    = excluded.institution,
            name           = excluded.name,
            type           = excluded.type,
            currency       = excluded.currency,
            balance_current= excluded.balance_current,
            last_synced    = excluded.last_synced
        """,
        {
            "id": account.get("id"),
            "institution": account.get("institution", "Unknown"),
            "name": account.get("name"),
            "type": account.get("type"),
            "currency": account.get("currency", "CAD"),
            "balance_current": account.get("balance_current", 0.0),
            "owner": account.get("owner", "personal"),
            "source": account.get("source", "wealthica"),
            "last_synced": account.get("last_synced"),
        },
    )
    conn.commit()


def get_accounts(conn: sqlite3.Connection) -> list[dict]:
    """Return all accounts as plain dicts."""
    rows = conn.execute("SELECT * FROM accounts ORDER BY type, institution, name").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------


def upsert_transaction(conn: sqlite3.Connection, txn: dict) -> bool:
    """Insert transaction if not already present.

    Returns True if new row was inserted, False if it already existed.
    For pending→cleared transitions, updates is_pending on the existing row.
    """
    existing = conn.execute(
        "SELECT id, is_pending FROM transactions WHERE id = ?", (txn["id"],)
    ).fetchone()
    if existing:
        if existing["is_pending"] and not txn.get("is_pending", 0):
            conn.execute("UPDATE transactions SET is_pending = 0 WHERE id = ?", (txn["id"],))
            conn.commit()
        return False

    conn.execute(
        """
        INSERT INTO transactions
            (id, account_id, date, amount, description, category, currency, owner, is_pending, source, note)
        VALUES
            (:id, :account_id, :date, :amount, :description, :category, :currency,
             :owner, :is_pending, :source, :note)
        """,
        {
            "id": txn["id"],
            "account_id": txn.get("account_id"),
            "date": txn.get("date"),
            "amount": txn.get("amount", 0.0),
            "description": txn.get("description", ""),
            "category": txn.get("category"),
            "currency": txn.get("currency", "CAD"),
            "owner": txn.get("owner", "personal"),
            "is_pending": int(txn.get("is_pending", 0)),
            "source": txn.get("source", "wealthica"),
            "note": txn.get("note"),
        },
    )
    conn.commit()
    return True


def get_transactions(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    owner: str | None = None,
    account_id: str | None = None,
) -> list[dict]:
    """Return transactions filtered by optional params, newest first."""
    clauses: list[str] = []
    params: list = []

    if start_date:
        clauses.append("date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("date <= ?")
        params.append(end_date)
    if owner:
        clauses.append("owner = ?")
        params.append(owner)
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM transactions {where} ORDER BY date DESC, id", params
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Recurring detection
# ---------------------------------------------------------------------------


_CADENCE_RANGES = [
    (5, 9, 7),  # weekly
    (12, 16, 14),  # biweekly
    (25, 35, 30),  # monthly
    (85, 100, 90),  # quarterly
]


def _normalize_merchant(desc: str) -> str:
    """Normalize a raw transaction description for merchant grouping."""
    s = desc.upper()
    s = re.sub(r"[.\-/]", " ", s)
    s = re.sub(r"\b\d{4,}\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:40].strip()


def _classify_cadence(median_days: float) -> int | None:
    for lo, hi, cadence in _CADENCE_RANGES:
        if lo <= median_days <= hi:
            return cadence
    return None


def detect_recurring(conn: sqlite3.Connection) -> int:
    """Analyse 180-day transaction history to find recurring charges.

    Clears and rebuilds the recurring table. Returns count of detected patterns.
    """
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    rows = conn.execute(
        "SELECT date, amount, description, owner, category FROM transactions "
        "WHERE date >= ? AND amount < 0 ORDER BY description, date",
        (cutoff,),
    ).fetchall()

    groups: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        norm = _normalize_merchant(row["description"])
        groups[norm].append((row["date"], row["amount"], row["owner"], row["category"]))

    conn.execute("DELETE FROM recurring")

    count = 0
    for merchant_norm, entries in groups.items():
        if len(entries) < 2:
            continue

        dates_sorted = sorted(e[0] for e in entries)
        intervals = [
            (date.fromisoformat(dates_sorted[i + 1]) - date.fromisoformat(dates_sorted[i])).days
            for i in range(len(dates_sorted) - 1)
        ]

        if not intervals:
            continue

        median_interval = statistics.median(intervals)
        cadence = _classify_cadence(median_interval)
        if cadence is None:
            continue

        amount_median = statistics.median(abs(e[1]) for e in entries)
        last_seen = max(dates_sorted)
        next_expected = (date.fromisoformat(last_seen) + timedelta(days=cadence)).isoformat()
        owner = entries[-1][2] or "personal"
        category = entries[-1][3]

        conn.execute(
            "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (merchant_norm, amount_median, cadence, last_seen, next_expected, owner, category),
        )
        count += 1

    conn.commit()
    return count


def get_recurring(conn: sqlite3.Connection) -> list[dict]:
    """Return all recurring charges sorted by next expected date."""
    rows = conn.execute("SELECT * FROM recurring ORDER BY next_expected").fetchall()
    return [dict(r) for r in rows]
