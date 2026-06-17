"""P1 push alerts — fired at end of each sync.

Alerts are appended to sync_state.pending_alerts as a JSON list.
Discord delivery is handled by the briefing layer (not here).

Three P1 alerts:
  large_unusual_charge — txn > $200 AND (new merchant OR >2σ above category norm)
  bill_shortfall       — bill due within 3 days AND amount > chequing balance
  duplicate_charge     — same merchant + same amount within 48h
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

_FINANCE_DIR = Path(__file__).parent
if str(_FINANCE_DIR.parents[1]) not in sys.path:
    sys.path.insert(0, str(_FINANCE_DIR.parents[1]))



# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_alerts(conn: sqlite3.Connection, new_txn_ids: list[str] | None = None) -> list[dict]:
    """Run all P1 alert checks. Returns list of new alert dicts and saves to sync_state."""
    alerts: list[dict] = []
    alerts.extend(_check_large_unusual(conn, new_txn_ids or []))
    alerts.extend(_check_bill_shortfall(conn))
    alerts.extend(_check_duplicate_charges(conn, new_txn_ids or []))
    if alerts:
        _append_alerts(conn, alerts)
    return alerts


# ---------------------------------------------------------------------------
# Alert: large / unusual charge
# ---------------------------------------------------------------------------


def _check_large_unusual(conn: sqlite3.Connection, new_txn_ids: list[str]) -> list[dict]:
    if not new_txn_ids:
        return []

    placeholders = ",".join("?" * len(new_txn_ids))
    rows = conn.execute(
        f"SELECT id, date, amount, description, category FROM transactions "
        f"WHERE id IN ({placeholders}) AND amount < -200",
        new_txn_ids,
    ).fetchall()

    alerts: list[dict] = []
    for row in rows:
        txn_id, date_str, amount, desc, category = (
            row["id"],
            row["date"],
            row["amount"],
            row["description"],
            row["category"],
        )

        # New merchant = first time this exact description appears (excluding this txn)
        prior_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE description = ? AND id != ?",
            (desc, txn_id),
        ).fetchone()[0]
        is_new_merchant = prior_count == 0

        # Amount > 2σ above category norm
        is_unusual_amount = False
        if category:
            cat_rows = conn.execute(
                "SELECT amount FROM transactions WHERE category = ? AND amount < 0 AND id != ?",
                (category, txn_id),
            ).fetchall()
            if len(cat_rows) >= 3:
                vals = [abs(r["amount"]) for r in cat_rows]
                mean = statistics.mean(vals)
                stdev = statistics.stdev(vals)
                is_unusual_amount = abs(amount) > mean + 2 * stdev

        if is_new_merchant or is_unusual_amount:
            reasons = []
            if is_new_merchant:
                reasons.append("new merchant")
            if is_unusual_amount:
                reasons.append("above category norm")
            alerts.append({
                "type": "large_unusual_charge",
                "txn_id": txn_id,
                "amount": amount,
                "description": desc,
                "date": date_str,
                "reason": ", ".join(reasons),
            })

    return alerts


# ---------------------------------------------------------------------------
# Alert: bill vs balance shortfall
# ---------------------------------------------------------------------------


def _check_bill_shortfall(conn: sqlite3.Connection) -> list[dict]:
    today = date.today()
    cutoff = (today + timedelta(days=3)).isoformat()
    today_str = today.isoformat()

    bills = conn.execute(
        "SELECT merchant_norm, amount_median, next_expected FROM recurring "
        "WHERE next_expected >= ? AND next_expected <= ?",
        (today_str, cutoff),
    ).fetchall()

    if not bills:
        return []

    chequing_row = conn.execute(
        "SELECT SUM(balance_current) FROM accounts WHERE type = 'bank'"
    ).fetchone()
    chequing = chequing_row[0] or 0.0

    alerts: list[dict] = []
    for row in bills:
        merchant, amount_median, next_expected = (
            row["merchant_norm"],
            row["amount_median"],
            row["next_expected"],
        )
        if amount_median > chequing:
            days_until = (date.fromisoformat(next_expected) - today).days
            alerts.append({
                "type": "bill_shortfall",
                "merchant": merchant,
                "amount": amount_median,
                "due_date": next_expected,
                "days_until": days_until,
                "chequing_balance": chequing,
                "shortfall": round(amount_median - chequing, 2),
            })

    return alerts


# ---------------------------------------------------------------------------
# Alert: duplicate charge
# ---------------------------------------------------------------------------


def _check_duplicate_charges(conn: sqlite3.Connection, new_txn_ids: list[str]) -> list[dict]:
    if not new_txn_ids:
        return []

    placeholders = ",".join("?" * len(new_txn_ids))
    rows = conn.execute(
        f"SELECT id, date, amount, description FROM transactions WHERE id IN ({placeholders})",
        new_txn_ids,
    ).fetchall()

    alerts: list[dict] = []
    for row in rows:
        txn_id, date_str, amount, desc = (
            row["id"],
            row["date"],
            row["amount"],
            row["description"],
        )
        txn_date = date.fromisoformat(date_str)
        window_start = (txn_date - timedelta(days=2)).isoformat()
        window_end = (txn_date + timedelta(days=2)).isoformat()

        dupes = conn.execute(
            "SELECT id FROM transactions WHERE description = ? AND amount = ? "
            "AND date BETWEEN ? AND ? AND id != ?",
            (desc, amount, window_start, window_end, txn_id),
        ).fetchall()

        if dupes:
            alerts.append({
                "type": "duplicate_charge",
                "txn_id": txn_id,
                "duplicate_ids": [r["id"] for r in dupes],
                "amount": amount,
                "description": desc,
                "date": date_str,
            })

    return alerts


# ---------------------------------------------------------------------------
# Persist alerts to sync_state
# ---------------------------------------------------------------------------


def _append_alerts(conn: sqlite3.Connection, alerts: list[dict]) -> None:
    now = datetime.now(UTC).isoformat()
    existing_row = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'pending_alerts'"
    ).fetchone()
    existing: list[dict] = json.loads(existing_row["value"]) if existing_row else []
    existing.extend(alerts)
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value, updated) VALUES ('pending_alerts', ?, ?)",
        (json.dumps(existing), now),
    )
    conn.commit()
