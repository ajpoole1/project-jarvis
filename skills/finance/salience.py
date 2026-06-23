"""Finance salience block — proactive signals for the morning briefing.

Called from brief.py to add a salience_block key to the finance snapshot.
Each signal is cheap (pure SQL/Python on local data) and returns structured dicts.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Excluded categories (mirrors skill.py — not imported to keep this standalone)
# ---------------------------------------------------------------------------

_EXCLUDED_CATEGORIES = frozenset(
    {
        "internal_transfer",
        "loan_payment",
        "investment_buy",
        "investment_sell",
        "investment_contribution",
        "investment_fee",
        "investment_grant",
        "investment_rebate",
        "inheritance_deposit",
        "insurance_reimbursement",
    }
)

_EXCL_CATS_SQL = (
    "'internal_transfer','loan_payment','investment_buy','investment_sell',"
    "'investment_contribution','investment_fee','investment_grant','investment_rebate',"
    "'inheritance_deposit','insurance_reimbursement'"
)
_EXCL_SQL = f"AND (category IS NULL OR category NOT IN ({_EXCL_CATS_SQL}))"


# ---------------------------------------------------------------------------
# Signal 1: cashflow projection (90-day forward)
# ---------------------------------------------------------------------------


def _cashflow_projection(conn) -> dict:
    """Return a simple 90-day forward cashflow estimate.

    Method:
      - Monthly income = trailing 3-month median of positive-amount transactions
        excluding non-operating flows
      - Monthly obligations = sum of recurring rows with cadence 25-35 days
        (fixed monthly bills, excluding transfers/investments)
      - Projected net/month = income - obligations
      - Runway = liquid / obligations (months)
    """
    today = date.today()
    three_months_ago = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    three_months_ago -= timedelta(days=60)

    income_rows = conn.execute(
        f"SELECT strftime('%Y-%m', date) as mo, SUM(amount) as total "
        f"FROM transactions "
        f"WHERE amount > 0 AND date >= ? {_EXCL_SQL} "
        f"GROUP BY mo ORDER BY mo",
        (three_months_ago.isoformat(),),
    ).fetchall()
    monthly_incomes = [r["total"] for r in income_rows if r["total"] and r["total"] > 0]
    monthly_income = statistics.median(monthly_incomes) if monthly_incomes else 0.0

    obligation_rows = conn.execute(
        "SELECT SUM(amount_median) as total FROM recurring "
        f"WHERE cadence_days BETWEEN 25 AND 35 "
        f"AND (category IS NULL OR category NOT IN ({_EXCL_CATS_SQL}))"
    ).fetchone()
    monthly_obligations = abs(obligation_rows["total"] or 0.0)

    bank_rows = conn.execute(
        "SELECT COALESCE(SUM(balance_current), 0) FROM accounts WHERE type = 'bank'"
    ).fetchone()
    liquid = bank_rows[0] or 0.0

    monthly_net = monthly_income - monthly_obligations
    runway_months = (liquid / monthly_obligations) if monthly_obligations > 0 else None

    return {
        "monthly_income_est": round(monthly_income, 2),
        "monthly_obligations_est": round(monthly_obligations, 2),
        "monthly_net_est": round(monthly_net, 2),
        "runway_months": round(runway_months, 1) if runway_months is not None else None,
        "alert": runway_months is not None and runway_months < 1.5,
    }


# ---------------------------------------------------------------------------
# Signal 2: large irregular charge forecast (quarterly/annual)
# ---------------------------------------------------------------------------


def _large_charge_forecast(conn, horizon_days: int = 60) -> list[dict]:
    """Return large irregular charges (cadence >60d) due within horizon_days.

    These are typically quarterly/annual subscriptions, insurance premiums, etc.
    Only flags items with amount_median >= $50.
    """
    today = date.today()
    cutoff = (today + timedelta(days=horizon_days)).isoformat()
    today_str = today.isoformat()

    rows = conn.execute(
        "SELECT merchant_norm, amount_median, cadence_days, next_expected "
        "FROM recurring "
        "WHERE cadence_days > 60 "
        "AND amount_median >= 50 "
        "AND next_expected >= ? AND next_expected <= ?",
        (today_str, cutoff),
    ).fetchall()

    result = []
    for r in rows:
        days_away = (date.fromisoformat(r["next_expected"]) - today).days
        result.append(
            {
                "merchant": r["merchant_norm"],
                "amount": round(r["amount_median"], 2),
                "due_date": r["next_expected"],
                "days_away": days_away,
                "cadence_days": r["cadence_days"],
            }
        )
    return sorted(result, key=lambda x: x["days_away"])


# ---------------------------------------------------------------------------
# Signal 3: category anomalies (this month vs 3-month median)
# ---------------------------------------------------------------------------


def _category_anomalies(conn, high_threshold: float = 1.20) -> list[dict]:
    """Return categories where current-month spend is >high_threshold × trailing median.

    Only returns HIGH anomalies (above threshold) — LOW signals are noise for a briefing.
    Requires at least 2 baseline months to avoid false positives on thin history.
    """
    today = date.today()
    period_start = today.replace(day=1).isoformat()
    period_end = today.isoformat()

    # Build 3 trailing months of history
    y, m = today.year, today.month
    baseline_periods = []
    for _ in range(9):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        ms = f"{y:04d}-{m:02d}-01"
        import calendar

        last_day = calendar.monthrange(y, m)[1]
        me = f"{y:04d}-{m:02d}-{last_day:02d}"
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM transactions WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL}",
            (ms, me),
        ).fetchone()[0]
        if cnt >= 5:
            baseline_periods.append((ms, me))
            if len(baseline_periods) >= 3:
                break

    if len(baseline_periods) < 2:
        return []

    monthly_by_cat: dict[str, list[float]] = defaultdict(list)
    for ms, me in baseline_periods:
        rows = conn.execute(
            f"SELECT COALESCE(category,'Uncategorized') as cat, SUM(ABS(amount)) as total "
            f"FROM transactions WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL} "
            f"GROUP BY cat",
            (ms, me),
        ).fetchall()
        seen = {r["cat"] for r in rows}
        for r in rows:
            monthly_by_cat[r["cat"]].append(r["total"])
        for cat in monthly_by_cat:
            if cat not in seen:
                monthly_by_cat[cat].append(0.0)

    curr_rows = conn.execute(
        f"SELECT COALESCE(category,'Uncategorized') as cat, SUM(ABS(amount)) as total "
        f"FROM transactions WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL} "
        f"GROUP BY cat",
        (period_start, period_end),
    ).fetchall()
    current = {r["cat"]: r["total"] for r in curr_rows}

    anomalies = []
    for cat, curr_val in current.items():
        hist = monthly_by_cat.get(cat, [])
        if not hist:
            continue
        median = statistics.median(hist)
        if median > 0 and curr_val / median > high_threshold:
            delta_pct = ((curr_val / median) - 1) * 100
            anomalies.append(
                {
                    "category": cat,
                    "current": round(curr_val, 2),
                    "median": round(median, 2),
                    "delta_pct": round(delta_pct, 1),
                }
            )

    return sorted(anomalies, key=lambda x: -x["delta_pct"])


# ---------------------------------------------------------------------------
# Signal 4: subscription drift
# ---------------------------------------------------------------------------


def _subscription_drift(conn) -> dict:
    """Return structured subscription audit: new / missing / stable counts + lists."""
    today = date.today()
    today_str = today.isoformat()
    cutoff_35 = (today - timedelta(days=35)).isoformat()
    cutoff_60 = (today - timedelta(days=60)).isoformat()

    rows = conn.execute("SELECT * FROM recurring ORDER BY merchant_norm").fetchall()

    new_items, missing_items, stable_items = [], [], []
    for r in rows:
        last_seen = r["last_seen"] or ""
        next_expected = r["next_expected"] or ""
        cadence = r["cadence_days"]
        if last_seen >= cutoff_35 and (cadence is None or cadence > 60):
            new_items.append({"merchant": r["merchant_norm"], "amount": r["amount_median"]})
        elif next_expected and next_expected < today_str and last_seen and last_seen < cutoff_60:
            missing_items.append({"merchant": r["merchant_norm"], "last_seen": r["last_seen"]})
        else:
            stable_items.append(r["merchant_norm"])

    return {
        "new": new_items,
        "missing": missing_items,
        "stable_count": len(stable_items),
        "alert": bool(new_items or missing_items),
    }


# ---------------------------------------------------------------------------
# Signal 5: duplicate charge detection
# ---------------------------------------------------------------------------


def _duplicate_charges(conn, window_days: int = 3, amount_tolerance: float = 0.05) -> list[dict]:
    """Flag potential duplicate charges: same merchant, amount within tolerance, within window_days.

    Looks back 30 days to catch recent duplicates.
    """
    today = date.today()
    since = (today - timedelta(days=30)).isoformat()

    rows = conn.execute(
        "SELECT id, date, amount, description FROM transactions "
        "WHERE amount < 0 AND date >= ? "
        "ORDER BY description, date",
        (since,),
    ).fetchall()

    txns = [dict(r) for r in rows]
    seen_pairs: set[tuple[str, str]] = set()
    duplicates = []

    for i, a in enumerate(txns):
        for b in txns[i + 1 :]:
            if a["description"] != b["description"]:
                continue
            days_apart = abs((date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days)
            if days_apart > window_days:
                continue
            a_amt, b_amt = abs(a["amount"]), abs(b["amount"])
            avg = (a_amt + b_amt) / 2
            if avg > 0 and abs(a_amt - b_amt) / avg <= amount_tolerance:
                pair_key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if pair_key not in seen_pairs:
                    seen_pairs.add(pair_key)
                    duplicates.append(
                        {
                            "description": a["description"],
                            "date_a": a["date"],
                            "date_b": b["date"],
                            "amount_a": round(a["amount"], 2),
                            "amount_b": round(b["amount"], 2),
                            "days_apart": days_apart,
                            "id_a": a["id"],
                            "id_b": b["id"],
                        }
                    )

    return duplicates


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def compute_salience_block(conn) -> dict:
    """Compute all 5 proactive signals. Returns a structured dict.

    Each signal is independently gated so a failure in one does not suppress others.
    """

    def _safe(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return None

    return {
        "cashflow_projection": _safe(_cashflow_projection, conn),
        "large_charge_forecast": _safe(_large_charge_forecast, conn) or [],
        "category_anomalies": _safe(_category_anomalies, conn) or [],
        "subscription_drift": _safe(_subscription_drift, conn),
        "duplicate_charges": _safe(_duplicate_charges, conn) or [],
    }
