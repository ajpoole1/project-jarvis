"""Finance brief — importable without skill.py to avoid circular dependencies.

Called by the morning-briefing assembler to get a finance snapshot dict:
    from skills.finance.brief import finance_brief
    data = finance_brief()

Returns a dict with these keys:
    liquid           — bank total minus CC outstanding (net liquid)
    cc_outstanding   — sum of credit account balances (what's owed on CCs)
    buffer_gap       — max(0, MORTGAGE_BUFFER_GOAL - bank_total)
    bills_due_7d     — list of {merchant, amount, due_date} for next 7 days
    shortfall        — True if any bill_due_7d amount > chequing balance
    top_spend_this_week — {category, amount} of highest spend category this week
    altaforma_this_week — total altaforma-owner spend this week
    anomalies        — list of unacknowledged alert dicts from sync_state
    data_age_hours   — hours since last sync, or None if never synced
"""

from __future__ import annotations

import json
import os

# ---------------------------------------------------------------------------
# Resolve imports — works both as a package module and when invoked standalone
# ---------------------------------------------------------------------------
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skills.finance.db import get_accounts, get_recurring, get_transactions, init_db  # noqa: E402

# ---------------------------------------------------------------------------
# Env loading — must work standalone (morning-briefing imports this directly)
# ---------------------------------------------------------------------------


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        for q in ('"', "'"):
            if value.startswith(q):
                end = value.find(q, 1)
                if end != -1:
                    value = value[1:end]
                break
        else:
            for sep in (" #", "\t#"):
                pos = value.find(sep)
                if pos != -1:
                    value = value[:pos].rstrip()
                    break
        if key:
            os.environ.setdefault(key, value)


_load_env(Path.home() / ".jarvis.env")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MORTGAGE_BUFFER_GOAL = 2140  # CAD — one Desjardins payment; will be dynamic in P2


def _db_path() -> Path:
    data_dir = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
    return data_dir / "finance.db"


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def finance_brief(db_path: str | None = None) -> dict:
    """Return a finance snapshot dict for the briefing assembler.

    Args:
        db_path: override the default finance.db path (used in tests).
    """
    path = Path(db_path) if db_path else _db_path()

    _empty = {
        "liquid": 0.0,
        "cc_outstanding": 0.0,
        "buffer_gap": float(MORTGAGE_BUFFER_GOAL),
        "bills_due_7d": [],
        "shortfall": False,
        "top_spend_this_week": None,
        "altaforma_this_week": 0.0,
        "anomalies": [],
        "data_age_hours": None,
    }

    if not path.exists():
        return _empty

    conn = init_db(str(path))
    try:
        return _build_brief(conn)
    finally:
        conn.close()


def _build_brief(conn) -> dict:
    accounts = get_accounts(conn)
    bank_total = sum(a["balance_current"] or 0 for a in accounts if a["type"] == "bank")
    cc_total = sum(a["balance_current"] or 0 for a in accounts if a["type"] == "credit")
    liquid = bank_total - cc_total
    buffer_gap = max(0.0, MORTGAGE_BUFFER_GOAL - bank_total)

    # Bills due in next 7 days
    today = date.today()
    cutoff = (today + timedelta(days=7)).isoformat()
    today_str = today.isoformat()
    recurring = get_recurring(conn)
    bills_due = [
        {
            "merchant": r["merchant_norm"],
            "amount": r["amount_median"],
            "due_date": r["next_expected"],
        }
        for r in recurring
        if r.get("next_expected") and today_str <= r["next_expected"] <= cutoff
    ]

    # Shortfall: any single bill > bank_total
    shortfall = any(b["amount"] > bank_total for b in bills_due)

    # Spend this week
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    week_txns = get_transactions(conn, start_date=week_start, end_date=today_str)
    spends: dict[str, float] = {}
    altaforma_week = 0.0
    for txn in week_txns:
        if txn["amount"] >= 0:
            continue
        if txn["owner"] == "altaforma":
            altaforma_week += abs(txn["amount"])
        cat = txn.get("category") or "Uncategorized"
        spends[cat] = spends.get(cat, 0.0) + abs(txn["amount"])

    top_spend = None
    if spends:
        top_cat = max(spends, key=lambda k: spends[k])
        top_spend = {"category": top_cat, "amount": round(spends[top_cat], 2)}

    # Anomalies (unacknowledged pending alerts from last sync)
    anomaly_row = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'pending_alerts'"
    ).fetchone()
    anomalies: list[dict] = json.loads(anomaly_row["value"]) if anomaly_row else []

    # Data age
    sync_row = conn.execute("SELECT value FROM sync_state WHERE key = 'last_sync'").fetchone()
    data_age_hours: float | None = None
    if sync_row:
        try:
            last_sync_dt = datetime.fromisoformat(sync_row["value"])
            if last_sync_dt.tzinfo is None:
                last_sync_dt = last_sync_dt.replace(tzinfo=UTC)
            data_age_hours = round((datetime.now(UTC) - last_sync_dt).total_seconds() / 3600, 1)
        except ValueError:
            pass

    return {
        "liquid": round(liquid, 2),
        "cc_outstanding": round(cc_total, 2),
        "buffer_gap": round(buffer_gap, 2),
        "bills_due_7d": bills_due,
        "shortfall": shortfall,
        "top_spend_this_week": top_spend,
        "altaforma_this_week": round(altaforma_week, 2),
        "anomalies": anomalies,
        "data_age_hours": data_age_hours,
    }
