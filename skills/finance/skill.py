"""Finance skill — personal CFO backed by Wealthica + SQLite.

Commands (P1):
  sync [--since DATE] [--full]
  import <file> [--account ID] [--owner personal|altaforma]
  accounts
  liquid
  spend [--month|--week|--from DATE --to DATE] [--owner personal|altaforma]
  top [N] [--period]
  search <term>
  net [--month|--from DATE --to DATE]
  recurring
  bills-due [--days N]
  tag <txn_id> <personal|altaforma> [--category CAT]
  scrape --bank <rbc|td> [--days 30] [--first-auth] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve sibling modules — works when invoked as `python skill.py <cmd>`
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skills.finance.alerts import check_alerts  # noqa: E402
from skills.finance.csv_import import (  # noqa: E402
    _rbc_payment_canonical,
    parse_csv,
    parse_csv_balances,
    parse_holdings,
    parse_holdings_balances,
)
from skills.finance.db import (  # noqa: E402
    _normalize_merchant,
    detect_recurring,
    get_accounts,
    get_recurring,
    get_transactions,
    init_db,
    recompute_balances,
    set_balance_anchor,
    upsert_account,
    upsert_position,
    upsert_transaction,
)
from skills.finance.scraper_errors import ScraperError, SessionExpiredError  # noqa: E402
from skills.finance.wealthica import get_institutions, get_token  # noqa: E402
from skills.finance.wealthica import get_transactions as wealthica_get_transactions  # noqa: E402

# ---------------------------------------------------------------------------
# Env + paths
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

DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "finance.db"

MORTGAGE_BUFFER_GOAL = 2140  # CAD — one Desjardins mortgage payment


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def _month_range() -> tuple[str, str]:
    today = date.today()
    start = today.replace(day=1).isoformat()
    return start, today.isoformat()


def _week_range() -> tuple[str, str]:
    today = date.today()
    start = (today - timedelta(days=today.weekday())).isoformat()
    return start, today.isoformat()


def _resolve_period(
    month: bool = False,
    week: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[str, str]:
    if from_date:
        return from_date, (to_date or date.today().isoformat())
    if week:
        return _week_range()
    # default and --month: current calendar month
    return _month_range()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _apply_rules(conn, txn_ids: list[str]) -> None:
    """Apply rules table to newly inserted transactions."""
    rules = conn.execute("SELECT pattern, owner, category FROM rules").fetchall()
    for rule in rules:
        pattern, owner, category = rule["pattern"], rule["owner"], rule["category"]
        set_parts = ["owner = ?"]
        params: list = [owner]
        if category:
            set_parts.append("category = ?")
            params.append(category)
        placeholders = ",".join("?" * len(txn_ids))
        params.extend([pattern, *txn_ids])
        conn.execute(
            f"UPDATE transactions SET {', '.join(set_parts)} "
            f"WHERE description LIKE ? AND id IN ({placeholders})",
            params,
        )
    conn.commit()


def _apply_rules_all(conn) -> int:
    """Apply all rules to the entire transactions table. Returns number of rows updated."""
    rules = conn.execute("SELECT pattern, owner, category FROM rules").fetchall()
    updated = 0
    for rule in rules:
        pattern, owner, category = rule["pattern"], rule["owner"], rule["category"]
        set_parts = ["owner = ?"]
        params: list = [owner]
        if category:
            set_parts.append("category = ?")
            params.append(category)
        params.append(pattern)
        cur = conn.execute(
            f"UPDATE transactions SET {', '.join(set_parts)} WHERE description LIKE ?",
            params,
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def _fmt_amount(amount: float) -> str:
    sign = "-" if amount < 0 else "+"
    return f"{sign}${abs(amount):,.2f}"


# ---------------------------------------------------------------------------
# Command: sync
# ---------------------------------------------------------------------------


def cmd_sync(conn, since: str | None = None, full: bool = False) -> str:
    client_id = os.environ.get("WEALTHICA_CLIENT_ID")
    secret = os.environ.get("WEALTHICA_SECRET")
    user = os.environ.get("WEALTHICA_USER")

    today = date.today().isoformat()

    if full:
        start_date = "2020-01-01"
    elif since:
        start_date = since
    else:
        last_row = conn.execute("SELECT value FROM sync_state WHERE key = 'last_sync'").fetchone()
        if last_row:
            start_date = last_row["value"][:10]
        else:
            start_date = (date.today() - timedelta(days=90)).isoformat()

    # Token — cached or fresh
    token: str | None = None
    if client_id:
        token_row = conn.execute("SELECT value FROM sync_state WHERE key = 'token'").fetchone()
        expiry_row = conn.execute(
            "SELECT value FROM sync_state WHERE key = 'token_expiry'"
        ).fetchone()
        now_iso = datetime.now(UTC).isoformat()
        if token_row and expiry_row and expiry_row["value"] > now_iso:
            token = token_row["value"]
        else:
            token = get_token(client_id, secret or "", user or "")
            expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value, updated) VALUES ('token', ?, ?)",
                (token, now_iso),
            )
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value, updated) VALUES ('token_expiry', ?, ?)",
                (expiry, now_iso),
            )
            conn.commit()
    # token=None → mock mode in wealthica module

    # Sync accounts
    institutions = get_institutions(token)
    now_str = datetime.now(UTC).isoformat()
    for inst in institutions:
        upsert_account(
            conn,
            {
                "id": inst.get("_id"),
                "institution": inst.get("institution"),
                "name": inst.get("name"),
                "type": inst.get("type"),
                "currency": inst.get("currency", "CAD"),
                "balance_current": inst.get("balance", 0.0),
                "source": "wealthica",
                "last_synced": now_str,
            },
        )

    # Sync transactions
    txns = wealthica_get_transactions(token, start_date, today)
    new_ids: list[str] = []
    for txn in txns:
        row = {
            "id": txn.get("_id"),
            "account_id": txn.get("account"),
            "date": txn.get("date"),
            "amount": txn.get("amount", 0.0),
            "description": txn.get("description", ""),
            "category": txn.get("category"),
            "currency": txn.get("currency", "CAD"),
            "is_pending": 1 if txn.get("pending") else 0,
            "source": "wealthica",
        }
        if upsert_transaction(conn, row):
            new_ids.append(row["id"])

    if new_ids:
        _apply_rules(conn, new_ids)

    detect_recurring(conn)
    alerts = check_alerts(conn, new_ids)

    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value, updated) VALUES ('last_sync', ?, ?)",
        (now_str, now_str),
    )
    conn.commit()

    mode = " [mock mode — set WEALTHICA_CLIENT_ID for live sync]" if not client_id else ""
    alert_note = f"  {len(alerts)} alert(s) pending." if alerts else ""
    return (
        f"Synced {len(new_ids)} new transactions across {len(institutions)} accounts. "
        f"Last sync: {now_str[:16].replace('T', ' ')} UTC.{mode}{alert_note}"
    )


# ---------------------------------------------------------------------------
# Command: import CSV
# ---------------------------------------------------------------------------


_ACCOUNT_NAMES: dict[str, str] = {
    "rbc-04330-5118989": "Personal Chequing",
    "rbc-03280-5365176": "Joint Chequing/Savings",
    "rbc-04330-5120290": "Personal Savings",
    "rbc-04330-5215470": "Joint Savings",
    "rbc-04330-5210174": "Nomi (Find & Save)",
    "rbc-01458-07839006001": "Royal Credit Line",
    "td-visa-1225": "TD Visa (1225)",
    "td-cc-1225": "TD Visa (1225)",
    "td-chq-5116": "TD Chequing (5116)",
    "mbna-8830": "MBNA Mastercard (8830)",
    "dsj-pca-710490": "Desjardins Chequing (PCA)",
    "dsj-ln1-710490": "Desjardins Mortgage (LN1)",
    "ws-WK19SSZN2CAD": "BDL Pension",
    "ws-WK2P9ZT49CAD": "Upgrade (RRSP)",
    "ws-WK3QRZ8Q0CAD": "Ellie's RESP",
    "ws-WK40VYS33CAD": "Joint Cash",
    "ws-HQ6YWF114CAD": "Crypto",
}

_PREFIX_MAP = [
    ("rbc-01458-", "RBC", "loan"),
    ("rbc-", "RBC", "bank"),
    ("td-visa-", "TD", "credit"),
    ("td-cc-", "TD", "credit"),
    ("td-chq-", "TD", "bank"),
    ("td-sav-", "TD", "bank"),
    ("td-", "TD", "bank"),
    ("mbna-", "MBNA", "credit"),
    ("rogers-", "Rogers", "credit"),
    ("dsj-ln", "Desjardins", "mortgage"),
    ("dsj-", "Desjardins", "bank"),
    ("ws-", "Wealthsimple", "investment"),
]


def _account_meta_from_id(account_id: str, owner: str, balance: float | None = None) -> dict:
    """Derive institution/name/type from account ID for auto-registration."""
    institution, acct_type = "Unknown", "bank"
    for prefix, inst, kind in _PREFIX_MAP:
        if account_id.startswith(prefix):
            institution, acct_type = inst, kind
            break
    return {
        "id": account_id,
        "institution": institution,
        "name": _ACCOUNT_NAMES.get(account_id, account_id),
        "type": acct_type,
        "currency": "CAD",
        "balance_current": balance,
        "source": "csv_import",
        "last_synced": None,
        "owner": owner,
    }


def cmd_import(conn, file_path: str, account_id: str | None = None, owner: str = "personal") -> str:
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    txns = parse_csv(str(path), account_id)
    balances = parse_csv_balances(str(path), account_id)
    inserted = skipped = 0
    seen_accounts: set[str] = set()
    for txn in txns:
        txn["owner"] = owner
        aid = txn.get("account_id", "")
        if aid and aid not in seen_accounts:
            seen_accounts.add(aid)
            upsert_account(conn, _account_meta_from_id(aid, owner, balances.get(aid)))
        if upsert_transaction(conn, txn):
            inserted += 1
        else:
            skipped += 1

    # Register any accounts that appeared in balances but had no transactions (e.g. crypto)
    for aid, bal in balances.items():
        if aid not in seen_accounts:
            upsert_account(conn, _account_meta_from_id(aid, owner, bal))

    recompute_balances(conn)
    detect_recurring(conn)

    return f"Imported {inserted} transactions ({skipped} skipped as duplicates)."


def cmd_import_holdings(conn, file_path: str, as_of: str | None = None) -> str:
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    # Extract date from filename if not supplied (e.g. holdings-report-2026-06-18.csv)
    if not as_of:
        import re as _re

        m = _re.search(r"(\d{4}-\d{2}-\d{2})", path.stem)
        as_of = m.group(1) if m else date.today().isoformat()

    positions = parse_holdings(str(path))
    if not positions:
        return "No positions found — check file format."

    for pos in positions:
        pos["as_of"] = as_of
        upsert_account(conn, _account_meta_from_id(pos["account_id"], "personal"))
        upsert_position(conn, pos)

    # Update balance_current and anchor for each investment account from holdings totals
    account_totals = parse_holdings_balances(positions)
    for aid, total in account_totals.items():
        set_balance_anchor(conn, aid, total, as_of)

    account_count = len(account_totals)
    lines = [
        f"Imported {len(positions)} positions across {account_count} account(s) as of {as_of}."
    ]
    for aid, total in sorted(account_totals.items()):
        name = _ACCOUNT_NAMES.get(aid, aid)
        lines.append(f"  {name}: ${total:,.2f} CAD")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: accounts
# ---------------------------------------------------------------------------


def cmd_accounts(conn) -> str:
    accounts = get_accounts(conn)
    if not accounts:
        return "No accounts found. Import CSVs with `finance import`."

    type_labels = [
        ("bank", "Bank accounts"),
        ("credit", "Credit cards"),
        ("investment", "Investments"),
        ("loan", "Loans"),
    ]
    groups: dict[str, list[dict]] = {t: [] for t, _ in type_labels}
    for acct in accounts:
        groups.setdefault(acct["type"] or "bank", []).append(acct)

    lines: list[str] = []
    totals: dict[str, float] = {}

    for type_key, label in type_labels:
        group = groups.get(type_key, [])
        if not group:
            continue
        lines.append(f"**{label}**")
        subtotal = 0.0
        for acct in group:
            bal = acct.get("balance_current") or 0.0
            synced = (acct.get("last_synced") or "never")[:10]
            lines.append(
                f"  {acct.get('institution', '?')} {acct.get('name', '?')}: ${bal:,.2f}  (synced {synced})"
            )
            subtotal += bal
        lines.append(f"  Subtotal: ${subtotal:,.2f}")
        lines.append("")
        totals[type_key] = subtotal

    bank_total = totals.get("bank", 0.0)
    cc_total = totals.get("credit", 0.0)
    net = bank_total - cc_total
    lines.append(f"Net liquid: ${net:,.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: liquid
# ---------------------------------------------------------------------------


def cmd_liquid(conn) -> str:
    accounts = get_accounts(conn)
    bank_accts = [a for a in accounts if a["type"] == "bank"]
    cc_accts = [a for a in accounts if a["type"] == "credit"]

    lines: list[str] = ["**Liquid position**"]

    bank_total = 0.0
    for acct in bank_accts:
        bal = acct.get("balance_current") or 0.0
        lines.append(f"  {acct.get('name', '?')}: ${bal:,.2f}")
        bank_total += bal

    if cc_accts:
        lines.append("")
    cc_total = 0.0
    for acct in cc_accts:
        bal = acct.get("balance_current") or 0.0
        lines.append(f"  {acct.get('name', '?')} (outstanding): ${bal:,.2f}")
        cc_total += bal

    # CC balances are already negative (liabilities) — add them, don't subtract.
    net = bank_total + cc_total
    lines.append("")
    lines.append(f"Net liquid: ${net:,.2f}")

    buffer_gap = max(0.0, MORTGAGE_BUFFER_GOAL - bank_total)
    lines.append(
        f"Mortgage buffer goal: ${MORTGAGE_BUFFER_GOAL:,.2f} | "
        + (f"Current gap: ${buffer_gap:,.2f}" if buffer_gap > 0 else "✓ Goal met")
    )

    sync_row = conn.execute("SELECT value FROM sync_state WHERE key = 'last_sync'").fetchone()
    if sync_row:
        try:
            last_dt = datetime.fromisoformat(sync_row["value"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            age_h = (datetime.now(UTC) - last_dt).total_seconds() / 3600
            if age_h > 24:
                lines.append(f"⚠️ Data is {age_h:.0f}h old — run `finance sync`")
        except ValueError:
            pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: spend
# ---------------------------------------------------------------------------


# SQL exclusion clause matching _EXCLUDED_CATEGORIES (used in compare / coda queries).
_EXCL_CATS_SQL = (
    "'internal_transfer','loan_payment','investment_buy','investment_sell',"
    "'investment_contribution','investment_fee','investment_grant','investment_rebate',"
    "'inheritance_deposit','insurance_reimbursement'"
)
_EXCL_SQL = f"AND (category IS NULL OR category NOT IN ({_EXCL_CATS_SQL}))"

# Categories excluded from spend/net/top by default.
# investment_income is intentionally absent — dividends/interest are real cash flows.
_TRANSFER_CATEGORIES = {"internal_transfer", "loan_payment"}
_INVESTMENT_FLOW_CATEGORIES = {
    "investment_buy",
    "investment_sell",
    "investment_contribution",
    "investment_fee",
    "investment_grant",
    "investment_rebate",
}
# Non-operating flows excluded from net income baseline:
#   inheritance_deposit — Ricky's money (~$1,300/mo); tracked separately, not salary
#   insurance_reimbursement — offsets an expense, not new income
_NON_OPERATING_INCOME = {"inheritance_deposit", "insurance_reimbursement"}
_EXCLUDED_CATEGORIES = _TRANSFER_CATEGORIES | _INVESTMENT_FLOW_CATEGORIES | _NON_OPERATING_INCOME


def cmd_spend(
    conn,
    period_start: str,
    period_end: str,
    owner: str | None = None,
    include_transfers: bool = False,
) -> str:
    txns = get_transactions(conn, start_date=period_start, end_date=period_end, owner=owner)
    spends: dict[str, float] = {}
    for txn in txns:
        if txn["amount"] >= 0:
            continue
        cat = txn.get("category") or "Uncategorized"
        if not include_transfers and cat in _EXCLUDED_CATEGORIES:
            continue
        spends[cat] = spends.get(cat, 0.0) + abs(txn["amount"])

    if not spends:
        owner_note = f" [{owner}]" if owner else ""
        return f"No spend found for {period_start} → {period_end}{owner_note}."

    lines = [f"**Spend: {period_start} → {period_end}**"]
    for cat, total in sorted(spends.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat:<28} ${total:>9,.2f}")
    lines.append(f"  {'─' * 39}")
    lines.append(f"  {'Total':<28} ${sum(spends.values()):>9,.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: top merchants
# ---------------------------------------------------------------------------


def cmd_top(conn, n: int, period_start: str, period_end: str) -> str:
    txns = get_transactions(conn, start_date=period_start, end_date=period_end)
    merchants: dict[str, float] = {}
    counts: dict[str, int] = {}
    for txn in txns:
        if txn["amount"] >= 0:
            continue
        if (txn.get("category") or "") in _EXCLUDED_CATEGORIES:
            continue
        desc = _rbc_payment_canonical(txn.get("description") or "Unknown")
        # Strip [PMT-NNNN] suffix — it's a dedup key, not a useful display label.
        desc = re.sub(r" \[PMT-\d{4}\]$", "", desc)
        norm = _normalize_merchant(desc)
        merchants[norm] = merchants.get(norm, 0.0) + abs(txn["amount"])
        counts[norm] = counts.get(norm, 0) + 1

    if not merchants:
        return f"No spend data for {period_start} → {period_end}."

    sorted_m = sorted(merchants.items(), key=lambda x: -x[1])[:n]
    lines = [f"**Top {n} merchants: {period_start} → {period_end}**"]
    for i, (merch, total) in enumerate(sorted_m, 1):
        lines.append(f"  {i:>2}. {merch:<32} ${total:>9,.2f}  ({counts[merch]}×)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: search
# ---------------------------------------------------------------------------


def cmd_search(conn, term: str) -> str:
    rows = conn.execute(
        "SELECT id, date, amount, description, category, account_id, owner FROM transactions "
        "WHERE description LIKE ? OR note LIKE ? ORDER BY date DESC LIMIT 50",
        (f"%{term}%", f"%{term}%"),
    ).fetchall()

    if not rows:
        return f"No transactions matching '{term}'."

    lines = [f"**Search: '{term}'** — {len(rows)} result(s)"]
    for row in rows:
        lines.append(
            f"  [{row['id'][:8]}] {row['date']}  {_fmt_amount(row['amount'])}  "
            f"{row['description']}  [{row['category'] or '?'}] [{row['owner']}]"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: net
# ---------------------------------------------------------------------------


def cmd_net(conn, period_start: str, period_end: str, include_transfers: bool = False) -> str:
    txns = get_transactions(conn, start_date=period_start, end_date=period_end)
    income = sum(
        t["amount"]
        for t in txns
        if t["amount"] > 0
        and (include_transfers or (t.get("category") or "") not in _EXCLUDED_CATEGORIES)
    )
    spend = sum(
        abs(t["amount"])
        for t in txns
        if t["amount"] < 0
        and (include_transfers or (t.get("category") or "") not in _EXCLUDED_CATEGORIES)
    )
    net = income - spend
    net_str = f"+${net:,.2f}" if net >= 0 else f"-${abs(net):,.2f}"
    return (
        f"**Net: {period_start} → {period_end}**\n"
        f"  In:  ${income:,.2f}\n"
        f"  Out: ${spend:,.2f}\n"
        f"  Net: {net_str}"
    )


# ---------------------------------------------------------------------------
# Command: compare
# ---------------------------------------------------------------------------


def cmd_compare(
    conn,
    period_start: str,
    period_end: str,
    baseline_months: int = 3,
    week_mode: bool = False,
) -> str:
    """Compare spend for a period against the trailing N-period median per category.

    --month mode: trailing N complete calendar months (default 3).
    --week mode:  trailing N complete calendar weeks (default 8).
    Sort: absolute delta descending.
    Verdict: normal (±20%), high (>20%), low (<20%), new (no history).
    """
    MIN_TXNS = 2 if week_mode else 5
    HIGH_THRESHOLD = 1.20
    LOW_THRESHOLD = 0.80

    ps = date.fromisoformat(period_start)
    baseline: list[tuple[str, str]] = []

    if week_mode:
        week_monday = ps - timedelta(days=ps.weekday())
        for i in range(1, baseline_months + 1):
            wk_start = week_monday - timedelta(weeks=i)
            wk_end = wk_start + timedelta(days=6)
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM transactions "
                f"WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL}",
                (wk_start.isoformat(), wk_end.isoformat()),
            ).fetchone()[0]
            if cnt >= MIN_TXNS:
                baseline.append((wk_start.isoformat(), wk_end.isoformat()))
    else:
        y, m = ps.year, ps.month
        for _ in range(baseline_months * 4):  # search at most 4× months to find N qualifying
            m -= 1
            if m == 0:
                m = 12
                y -= 1
            ms = f"{y:04d}-{m:02d}-01"
            last_day = calendar.monthrange(y, m)[1]
            me = f"{y:04d}-{m:02d}-{last_day:02d}"
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM transactions "
                f"WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL}",
                (ms, me),
            ).fetchone()[0]
            if cnt >= MIN_TXNS:
                baseline.append((ms, me))
                if len(baseline) >= baseline_months:
                    break

    if not baseline:
        return "Not enough history for comparison — import more months first."

    monthly_by_cat: dict[str, list[float]] = defaultdict(list)
    for ms, me in baseline:
        rows = conn.execute(
            f"SELECT COALESCE(category,'Uncategorized') as cat, SUM(ABS(amount)) as total "
            f"FROM transactions "
            f"WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL} "
            f"GROUP BY cat",
            (ms, me),
        ).fetchall()
        seen_cats = set()
        for r in rows:
            monthly_by_cat[r["cat"]].append(r["total"])
            seen_cats.add(r["cat"])
        for cat in monthly_by_cat:
            if cat not in seen_cats:
                monthly_by_cat[cat].append(0.0)

    current_rows = conn.execute(
        f"SELECT COALESCE(category,'Uncategorized') as cat, SUM(ABS(amount)) as total "
        f"FROM transactions "
        f"WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL} "
        f"GROUP BY cat",
        (period_start, period_end),
    ).fetchall()
    current = {r["cat"]: r["total"] for r in current_rows}

    if not current:
        return f"No spend data for {period_start} → {period_end}."

    def _med(cat: str) -> float | None:
        hist = monthly_by_cat.get(cat, [])
        return statistics.median(hist) if hist else None

    all_cats = sorted(
        set(current) | set(monthly_by_cat),
        key=lambda c: -abs(current.get(c, 0.0) - (_med(c) or 0.0)),
    )

    n_actual = len(baseline)
    if week_mode:
        baseline_label = f"{n_actual}-week median"
        period_note = "" if n_actual >= baseline_months else f" (baseline: {n_actual} weeks)"
    else:
        baseline_label = f"{baseline[-1][0][:7]}–{baseline[0][0][:7]} median"
        period_note = "" if n_actual >= baseline_months else f" (baseline: {n_actual} months)"

    ps_dt = date.fromisoformat(period_start)
    pe_dt = date.fromisoformat(period_end)
    period_label = f"{ps_dt.strftime('%b')} {ps_dt.day}–{pe_dt.day}"
    mode_label = "week" if week_mode else "month"

    lines = [
        f"**Spend comparison ({mode_label})**",
        f"  {'Category':<22}  {'This period':>11}  {'Median':>9}  {'Δ':>7}  Verdict",
        f"  {'─'*70}",
    ]

    for cat in all_cats:
        curr = current.get(cat, 0.0)
        hist = monthly_by_cat.get(cat, [])
        median = statistics.median(hist) if hist else None

        if median is None or median == 0:
            verdict = "new" if curr > 0 else ""
            delta_str = "    n/a"
            median_str = "      n/a"
        else:
            ratio = curr / median
            delta_pct = (ratio - 1) * 100
            delta_str = f"{delta_pct:>+6.0f}%"
            median_str = f"${median:>8,.0f}"
            if ratio > HIGH_THRESHOLD:
                verdict = f"▲ HIGH  (+{delta_pct:.0f}%)"
            elif curr == 0:
                verdict = "— absent"
            elif ratio < LOW_THRESHOLD:
                verdict = f"▼ low   ({delta_pct:.0f}%)"
            else:
                verdict = "✓ normal"

        if curr == 0 and (median is None or median == 0):
            continue

        curr_str = f"${curr:>9,.0f}" if curr else "         —"
        lines.append(f"  {cat:<22}  {curr_str}  {median_str}  {delta_str}  {verdict}")

    total_curr = sum(current.values())
    total_medians = [statistics.median(v) for v in monthly_by_cat.values() if v]
    total_median = sum(total_medians)
    total_delta = ((total_curr / total_median) - 1) * 100 if total_median else 0
    lines += [
        f"  {'─'*70}",
        f"  {'Total':<22}  ${total_curr:>9,.0f}  ${total_median:>8,.0f}  {total_delta:>+6.0f}%",
        "",
        f"  Period: {period_label}{period_note} | Baseline: {baseline_label}",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: debt
# ---------------------------------------------------------------------------

# Static obligation metadata — balance_current comes from DB where available.
# monthly_payment: known fixed payment (0 = variable/manual).
# payoff_date: known fixed end date (None = computed from balance/payment).
# notes: shown in output.
_DEBT_META = [
    {
        "account_id": "td-visa-1225",
        "label": "TD Visa (1225)",
        "monthly_payment": 0,
        "payoff_date": None,
        "priority": 1,
        "notes": "Active paydown — Ricky's money + paycheque lump incoming",
    },
    {
        "account_id": "mbna-8830",
        "label": "MBNA Mastercard (8830)",
        "monthly_payment": 0,
        "payoff_date": None,
        "priority": 2,
        "notes": "After TD Visa cleared",
    },
    {
        "account_id": "rbc-01458-07839006001",
        "label": "RBC Line of Credit",
        "monthly_payment": 0,
        "payoff_date": None,
        "priority": 3,
        "notes": "After MBNA cleared",
    },
    {
        "account_id": None,
        "label": "Polina's LoC (external)",
        "balance": -25000.00,
        "monthly_payment": 600,
        "payoff_date": None,
        "priority": 4,
        "notes": "$300/cheque × 2 from joint chequing; Polina's account, no DB visibility",
    },
    {
        "account_id": None,
        "label": "Car loan (TD)",
        "balance": None,
        "monthly_payment": 237,
        "payoff_date": "2027",
        "priority": 5,
        "notes": "Fixed schedule — do not accelerate",
    },
    {
        "account_id": "dsj-ln1-710490",
        "label": "Mortgage (Desjardins LN1)",
        "monthly_payment": 3202,
        "payoff_date": "2049-06-10",
        "priority": 6,
        "notes": "Term renewal 2027-09-10 @ 4.990% — watch refinance window late 2026",
    },
]


def cmd_debt(conn) -> str:
    # Pull live balances from DB
    acct_rows = {r["id"]: r["balance_current"] for r in get_accounts(conn)}

    today = date.today()
    lines = ["**Debt stack** (priority order)"]
    lines.append(
        "  %-26s  %12s  %10s  %12s  %s" % ("Debt", "Balance", "Monthly", "Payoff", "Notes")
    )
    lines.append("  " + "─" * 90)

    total_owed = 0.0

    for d in _DEBT_META:
        aid = d.get("account_id")
        if aid and aid in acct_rows and acct_rows[aid] is not None:
            balance = acct_rows[aid]
        else:
            balance = d.get("balance")

        monthly = d.get("monthly_payment", 0)

        # Compute projected payoff if we have balance + monthly payment
        payoff_str = d.get("payoff_date") or ""
        if not payoff_str and balance is not None and monthly > 0:
            months_left = abs(balance) / monthly
            proj = date(
                today.year + (today.month - 1 + round(months_left)) // 12,
                ((today.month - 1 + round(months_left)) % 12) + 1,
                1,
            )
            payoff_str = proj.strftime("%Y-%m")

        bal_str = (f"${format(int(abs(balance)), ',')}") if balance is not None else "unknown"
        mo_str = (f"${format(monthly, ',')}/mo") if monthly else "variable"

        lines.append(
            "  %d. %-24s  %12s  %10s  %12s  %s"
            % (d["priority"], d["label"], bal_str, mo_str, payoff_str, d.get("notes", ""))
        )

        if balance is not None:
            total_owed += abs(balance)

    # Mortgage may not have been counted above if its balance was None in _DEBT_META
    # but is present in the DB — ensure it's included in total
    mortgage_db = acct_rows.get("dsj-ln1-710490")
    mortgage_bal = abs(mortgage_db) if mortgage_db is not None else 562984.65
    if mortgage_db is not None and abs(mortgage_db) not in [total_owed]:
        # Re-derive: sum everything except mortgage, then add mortgage separately
        pass
    ex_mortgage = sum(
        abs(acct_rows[d["account_id"]])
        if d.get("account_id") and acct_rows.get(d["account_id"]) is not None
        else abs(d.get("balance") or 0)
        for d in _DEBT_META
        if d.get("account_id") != "dsj-ln1-710490"
    )
    lines.append("  " + "─" * 90)
    lines.append("  %-26s  $%s" % ("Total owed (ex-mortgage)", format(int(ex_mortgage), ",")))
    lines.append(
        "  %-26s  $%s" % ("Total owed (inc-mortgage)", format(int(ex_mortgage + mortgage_bal), ","))
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: recurring
# ---------------------------------------------------------------------------


def cmd_recurring(conn) -> str:
    rows = get_recurring(conn)
    if not rows:
        detect_recurring(conn)
        rows = get_recurring(conn)
    if not rows:
        return "No recurring charges detected. Import more transaction history to analyse."

    today = date.today()
    lines = ["**Recurring charges**"]
    lines.append(
        f"  {'Merchant':<32} {'Amount':>9}  {'Cadence':>11}  {'Next Expected':<14}  Status"
    )
    lines.append("  " + "─" * 78)

    for r in rows:
        merchant = (r["merchant_norm"] or "")[:30]
        amount = f"${r['amount_median']:,.2f}"
        cadence = f"every {r['cadence_days']}d"
        next_exp = r.get("next_expected") or "unknown"

        status = ""
        if r.get("last_seen") and r.get("cadence_days"):
            threshold = date.fromisoformat(r["last_seen"]) + timedelta(
                days=int(r["cadence_days"] * 1.5)
            )
            if today > threshold:
                status = "⚠️ possibly lapsed"

        lines.append(f"  {merchant:<32} {amount:>9}  {cadence:>11}  {next_exp:<14}  {status}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: bills-due
# ---------------------------------------------------------------------------


def cmd_bills_due(conn, days: int = 7) -> str:
    today = date.today()
    cutoff = (today + timedelta(days=days)).isoformat()
    today_str = today.isoformat()

    bills = conn.execute(
        "SELECT merchant_norm, amount_median, next_expected FROM recurring "
        "WHERE next_expected >= ? AND next_expected <= ?",
        (today_str, cutoff),
    ).fetchall()

    chequing_row = conn.execute(
        "SELECT SUM(balance_current) FROM accounts WHERE type = 'bank'"
    ).fetchone()
    chequing = chequing_row[0] or 0.0

    if not bills:
        return f"No bills due in the next {days} days."

    total = sum(b["amount_median"] for b in bills)
    shortfall = max(0.0, total - chequing)

    lines = [f"**Bills due — next {days} days**  (chequing: ${chequing:,.2f})"]
    for row in bills:
        days_until = (date.fromisoformat(row["next_expected"]) - today).days
        lines.append(
            f"  {(row['merchant_norm'] or ''):<32} ${row['amount_median']:>9,.2f}  "
            f"due {row['next_expected']} ({days_until}d)"
        )
    lines.append(f"\n  Total: ${total:,.2f}")
    if shortfall > 0:
        lines.append(f"  ⚠️ Shortfall: ${shortfall:,.2f} — top up chequing")
    else:
        lines.append("  ✓ Chequing balance covers upcoming bills")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: tag
# ---------------------------------------------------------------------------


def cmd_tag(conn, txn_id: str, owner: str, category: str | None = None) -> str:
    row = conn.execute(
        "SELECT id, date, amount, description FROM transactions WHERE id = ?",
        (txn_id,),
    ).fetchone()

    if not row:
        # Partial prefix match
        rows = conn.execute(
            "SELECT id, date, amount, description FROM transactions WHERE id LIKE ?",
            (f"{txn_id}%",),
        ).fetchall()
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            return f"Ambiguous ID prefix '{txn_id}' — {len(rows)} matches. Be more specific."
        else:
            return f"Transaction '{txn_id}' not found."

    tid = row["id"]
    set_parts = ["owner = ?"]
    params: list = [owner]
    if category:
        set_parts.append("category = ?")
        params.append(category)
    params.append(tid)

    conn.execute(f"UPDATE transactions SET {', '.join(set_parts)} WHERE id = ?", params)
    conn.commit()

    cat_str = f", category={category}" if category else ""
    return (
        f"✓ [{tid[:8]}] {row['date']}  {_fmt_amount(row['amount'])}  "
        f"{row['description']} → owner={owner}{cat_str}"
    )


# ---------------------------------------------------------------------------
# Commands: rule add / list / remove
# ---------------------------------------------------------------------------

_VALID_OWNERS = {"personal", "altaforma"}


def cmd_rule_add(conn, pattern: str, owner: str, category: str | None = None) -> str:
    if not pattern.strip():
        return "Error: pattern must be non-empty."
    if owner not in _VALID_OWNERS:
        return f"Error: owner must be 'personal' or 'altaforma', got '{owner}'."
    today = date.today().isoformat()
    cur = conn.execute(
        "INSERT INTO rules (pattern, owner, category, created) VALUES (?, ?, ?, ?)",
        (pattern, owner, category, today),
    )
    conn.commit()
    rule_id = cur.lastrowid
    applied = _apply_rules_all(conn)
    target = f"{owner} / {category}" if category else owner
    return f'Rule added (id={rule_id}): "{pattern}" → {target}. Applied to {applied} existing transaction(s).'


def cmd_rule_list(conn) -> str:
    rows = conn.execute(
        "SELECT id, pattern, owner, category, created FROM rules ORDER BY id"
    ).fetchall()
    if not rows:
        return "No rules defined."
    col_widths = {
        "id": max(2, max(len(str(r["id"])) for r in rows)),
        "pattern": max(7, max(len(r["pattern"]) for r in rows)),
        "owner": max(5, max(len(r["owner"]) for r in rows)),
        "category": max(8, max(len(r["category"] or "") for r in rows)),
        "created": 10,
    }

    def _pad(val, width):
        return str(val or "").ljust(width)

    header = (
        f"{'id'.ljust(col_widths['id'])}  "
        f"{'pattern'.ljust(col_widths['pattern'])}  "
        f"{'owner'.ljust(col_widths['owner'])}  "
        f"{'category'.ljust(col_widths['category'])}  "
        f"created"
    )
    sep = (
        "  ".join("-" * col_widths[k] for k in ("id", "pattern", "owner", "category"))
        + "  ----------"
    )
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"{_pad(r['id'], col_widths['id'])}  "
            f"{_pad(r['pattern'], col_widths['pattern'])}  "
            f"{_pad(r['owner'], col_widths['owner'])}  "
            f"{_pad(r['category'], col_widths['category'])}  "
            f"{r['created'] or ''}"
        )
    return "\n".join(lines)


def cmd_rule_remove(conn, rule_id: int) -> str:
    row = conn.execute(
        "SELECT id, pattern, owner, category FROM rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if not row:
        return f"Rule {rule_id} not found."
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()
    if row["category"]:
        target = f"{row['owner']}/{row['category']}"
    else:
        target = row["owner"]
    return (
        f'Rule {rule_id} removed ("{row["pattern"]}" {target}). '
        f"Run finance apply-rules to re-categorize if needed."
    )


# ---------------------------------------------------------------------------
# Command: scrape
# ---------------------------------------------------------------------------


def cmd_scrape(
    conn,
    bank: str,
    days: int = 30,
    first_auth: bool = False,
    dry_run: bool = False,
) -> str:
    if bank == "rbc":
        from skills.finance import scraper_rbc  # lazy import — playwright optional

        async def _run():
            return await scraper_rbc.fetch_transactions(days=days, force_headful=first_auth)

        try:
            txns = asyncio.run(_run())
        except SessionExpiredError as exc:
            return str(exc)
        except ScraperError as exc:
            return f"Scrape failed: {exc}"
    elif bank == "td":
        from skills.finance import scraper_td

        async def _run_td():
            return await scraper_td.fetch_transactions(days=days)

        try:
            asyncio.run(_run_td())
        except NotImplementedError as exc:
            return str(exc)
        return "TD scraper not yet implemented."
    else:
        return f"Unknown bank: {bank}. Supported: rbc, td"

    if dry_run:
        lines = [f"[dry-run] {len(txns)} transactions from {bank.upper()}:"]
        for t in txns[:20]:
            lines.append(
                f"  {t['date']}  {_fmt_amount(t['amount'])}  {t['description']}  [{t['account']}]"
            )
        if len(txns) > 20:
            lines.append(f"  … {len(txns) - 20} more")
        return "\n".join(lines)

    inserted = skipped = 0
    for txn in txns:
        row = {
            "id": txn["id"],
            "account_id": txn.get("account_id", f"{bank}-unknown"),
            "date": txn["date"],
            "amount": txn["amount"],
            "description": txn["description"],
            "category": txn.get("category"),
            "currency": txn.get("currency", "CAD"),
            "owner": "personal",
            "is_pending": txn.get("is_pending", 0),
            "source": txn.get("source", f"scraper_{bank}"),
            "note": None,
        }
        if upsert_transaction(conn, row):
            inserted += 1
        else:
            skipped += 1

    next_bank = "td" if bank == "rbc" else ""
    next_hint = f"  Next: finance scrape --bank {next_bank}" if next_bank else ""
    return (
        f"Scraped {len(txns)} transactions from {bank.upper()} "
        f"({inserted} new, {skipped} already in DB).{next_hint}"
    )


# ---------------------------------------------------------------------------
# Command: runway
# ---------------------------------------------------------------------------


def cmd_runway(conn, include_inheritance: bool = False) -> str:
    """Liquid / monthly obligations. monthly_obligations = recurring where cadence ~30d."""
    accounts = get_accounts(conn)
    bank_total = sum(a.get("balance_current") or 0.0 for a in accounts if a["type"] == "bank")
    cc_total = sum(a.get("balance_current") or 0.0 for a in accounts if a["type"] == "credit")
    liquid = bank_total - cc_total

    if include_inheritance:
        liquid += 1300.0

    rows = conn.execute(
        "SELECT merchant_norm, amount_median, category FROM recurring "
        "WHERE cadence_days BETWEEN 25 AND 35",
    ).fetchall()
    qualified = [r for r in rows if (r["category"] or "") not in _EXCLUDED_CATEGORIES]

    if not qualified:
        return (
            "No monthly recurring obligations detected. "
            "Run `finance import` then `finance recurring` to build patterns."
        )

    monthly_total = sum(r["amount_median"] for r in qualified)
    runway = liquid / monthly_total if monthly_total > 0 else float("inf")

    inh_note = " (incl. $1,300 inheritance)" if include_inheritance else ""
    lines = [
        f"**Runway: {runway:.1f} months**",
        f"  Liquid: ${liquid:,.2f}{inh_note}",
        f"  Monthly obligations: ${monthly_total:,.2f}",
        "",
        "  Top obligations:",
    ]
    for r in sorted(qualified, key=lambda x: -x["amount_median"])[:5]:
        lines.append(f"    {(r['merchant_norm'] or '?'):<32} ${r['amount_median']:>9,.2f}/mo")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: altaforma
# ---------------------------------------------------------------------------


def cmd_altaforma(conn, quarter: bool = False) -> str:
    """Altaforma transaction summary grouped by category."""
    today = date.today()
    if quarter:
        q = (today.month - 1) // 3 + 1
        q_start_month = (q - 1) * 3 + 1
        period_start = date(today.year, q_start_month, 1).isoformat()
        period_label = f"Q{q} {today.year}"
    else:
        period_start = date(today.year, 1, 1).isoformat()
        period_label = str(today.year)
    period_end = today.isoformat()

    rows = conn.execute(
        "SELECT COALESCE(category, 'Uncategorized') as cat, SUM(amount) as total "
        "FROM transactions "
        "WHERE owner = 'altaforma' AND date >= ? AND date <= ? "
        "GROUP BY cat ORDER BY total ASC",
        (period_start, period_end),
    ).fetchall()

    if not rows:
        return f"No Altaforma transactions for {period_label}."

    spend_rows = [r for r in rows if r["total"] < 0]
    income_rows = [r for r in rows if r["total"] >= 0]

    lines = [f"**Altaforma — {period_label}**"]
    for r in spend_rows:
        lines.append(f"  {r['cat']:<28} ${abs(r['total']):>9,.2f}")

    total_spend = sum(abs(r["total"]) for r in spend_rows)
    total_income = sum(r["total"] for r in income_rows)

    lines += [
        f"  {'─' * 39}",
        f"  {'Total spend':<28} ${total_spend:>9,.2f}",
    ]
    if income_rows:
        total_net = total_income - total_spend
        net_str = f"+${total_net:,.2f}" if total_net >= 0 else f"-${abs(total_net):,.2f}"
        lines.append(f"  {'Revenue':<28} ${total_income:>9,.2f}")
        lines.append(f"  {'Net':<28} {net_str:>10}")
    else:
        lines.append("  Revenue: none recorded — tag income transactions owner=altaforma")

    lines.append(f"\n  Period: {period_start} → {period_end}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: subs-audit
# ---------------------------------------------------------------------------


def cmd_subs_audit(conn) -> str:
    """Audit recurring subscriptions: NEW / MISSING / CHANGED / Stable."""
    today = date.today()
    today_str = today.isoformat()
    cutoff_35 = (today - timedelta(days=35)).isoformat()
    cutoff_60 = (today - timedelta(days=60)).isoformat()

    rows = conn.execute("SELECT * FROM recurring ORDER BY merchant_norm").fetchall()

    if not rows:
        return (
            "No recurring data. Run `finance import` then `finance recurring` to detect patterns."
        )

    new_items, missing_items, stable_items = [], [], []
    for r in rows:
        last_seen = r["last_seen"] or ""
        next_expected = r["next_expected"] or ""
        cadence = r["cadence_days"]
        if last_seen >= cutoff_35 and (cadence is None or cadence > 60):
            new_items.append(r)
        elif next_expected and next_expected < today_str and last_seen and last_seen < cutoff_60:
            missing_items.append(r)
        else:
            stable_items.append(r)

    lines = []
    if new_items:
        lines.append(f"**NEW ({len(new_items)})**")
        for r in new_items:
            lines.append(f"  {(r['merchant_norm'] or '?'):<32} ${r['amount_median']:>9,.2f}")
    else:
        lines.append("**NEW (0)**")

    if missing_items:
        lines.append(f"**MISSING ({len(missing_items)})**")
        for r in missing_items:
            lines.append(f"  {(r['merchant_norm'] or '?'):<32}  last seen {r['last_seen']}")
    else:
        lines.append("**MISSING (0)**")

    lines.append("**CHANGED (0)**  (insufficient prior-amount data)")
    lines.append(f"**Stable: {len(stable_items)}**")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Briefing coda helpers (not CLI subcommands)
# ---------------------------------------------------------------------------


def finance_weekly_coda() -> str | None:
    """2-3 line prose for Sunday briefing: top-3 week deltas + Altaforma spend.

    Returns None if DB unavailable or no relevant data.
    """
    try:
        if not DB_PATH.exists():
            return None
        conn = init_db(str(DB_PATH))
        try:
            week_start, week_end = _week_range()

            curr_rows = conn.execute(
                f"SELECT COALESCE(category,'Uncategorized') as cat, SUM(ABS(amount)) as total "
                f"FROM transactions "
                f"WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL} "
                f"GROUP BY cat",
                (week_start, week_end),
            ).fetchall()
            current = {r["cat"]: r["total"] for r in curr_rows}

            today_dt = date.fromisoformat(week_start)
            week_monday = today_dt - timedelta(days=today_dt.weekday())
            monthly_by_cat: dict[str, list[float]] = defaultdict(list)
            for i in range(1, 9):
                ws = (week_monday - timedelta(weeks=i)).isoformat()
                we = (week_monday - timedelta(weeks=i) + timedelta(days=6)).isoformat()
                rows = conn.execute(
                    f"SELECT COALESCE(category,'Uncategorized') as cat, SUM(ABS(amount)) as total "
                    f"FROM transactions "
                    f"WHERE date >= ? AND date <= ? AND amount < 0 {_EXCL_SQL} "
                    f"GROUP BY cat",
                    (ws, we),
                ).fetchall()
                seen = {r["cat"] for r in rows}
                for r in rows:
                    monthly_by_cat[r["cat"]].append(r["total"])
                for cat in monthly_by_cat:
                    if cat not in seen:
                        monthly_by_cat[cat].append(0.0)

            deltas = []
            for cat, curr_val in current.items():
                hist = monthly_by_cat.get(cat, [])
                med = statistics.median(hist) if hist else 0.0
                delta = curr_val - med
                if abs(delta) > 5:
                    deltas.append((cat, curr_val, med, delta))
            deltas.sort(key=lambda x: -abs(x[3]))

            alta = (
                conn.execute(
                    "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
                    "WHERE owner='altaforma' AND amount < 0 AND date >= ? AND date <= ?",
                    (week_start, week_end),
                ).fetchone()[0]
                or 0.0
            )

            parts = []
            if deltas:
                top3 = deltas[:3]
                pieces = [f"{cat} {'▲' if d > 0 else '▼'}${abs(d):,.0f}" for cat, _, _, d in top3]
                parts.append("Spend vs median: " + " · ".join(pieces))
            elif current:
                top_cats = sorted(current.items(), key=lambda x: -x[1])[:3]
                parts.append("This week: " + " · ".join(f"{c} ${v:,.0f}" for c, v in top_cats))
            if alta > 0:
                parts.append(f"Altaforma this week: ${alta:,.2f}")

            return "\n".join(parts) if parts else None
        finally:
            conn.close()
    except Exception:
        return None


def finance_monthly_coda() -> str | None:
    """3 compact lines for 1st-of-month briefing: prior-month net, subs-audit, GST.

    Returns None if DB unavailable.
    """
    try:
        if not DB_PATH.exists():
            return None
        conn = init_db(str(DB_PATH))
        try:
            today = date.today()
            first_this = today.replace(day=1)
            pm_end = first_this - timedelta(days=1)
            pm_start = pm_end.replace(day=1)

            txns = conn.execute(
                "SELECT amount, category FROM transactions WHERE date >= ? AND date <= ?",
                (pm_start.isoformat(), pm_end.isoformat()),
            ).fetchall()
            income = sum(
                t["amount"]
                for t in txns
                if t["amount"] > 0 and (t["category"] or "") not in _EXCLUDED_CATEGORIES
            )
            spend = sum(
                abs(t["amount"])
                for t in txns
                if t["amount"] < 0 and (t["category"] or "") not in _EXCLUDED_CATEGORIES
            )
            net = income - spend
            net_str = f"+${net:,.0f}" if net >= 0 else f"-${abs(net):,.0f}"
            month_label = pm_end.strftime("%B")

            today_str = today.isoformat()
            cutoff_35 = (today - timedelta(days=35)).isoformat()
            cutoff_60 = (today - timedelta(days=60)).isoformat()
            rec_rows = conn.execute("SELECT * FROM recurring").fetchall()
            n_new = sum(
                1
                for r in rec_rows
                if (r["last_seen"] or "") >= cutoff_35
                and (r["cadence_days"] is None or r["cadence_days"] > 60)
            )
            n_missing = sum(
                1
                for r in rec_rows
                if (r["next_expected"] or "") < today_str and (r["last_seen"] or "") < cutoff_60
            )
            n_stable = len(rec_rows) - n_new - n_missing

            q = (today.month - 1) // 3 + 1
            q_start = date(today.year, (q - 1) * 3 + 1, 1).isoformat()
            gst_row = conn.execute(
                "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
                "WHERE date >= ? AND date <= ? AND category IN ('taxes','gst','hst','qst')",
                (q_start, today_str),
            ).fetchone()
            gst_paid = gst_row[0] if gst_row else 0.0
            gst_status = (
                f"GST Q{q}: ${gst_paid:,.0f} paid" if gst_paid > 0 else f"GST Q{q}: none recorded"
            )

            return "\n".join(
                [
                    f"{month_label} net: {net_str} (in ${income:,.0f} / out ${spend:,.0f})",
                    f"Subs: {n_new} new · {n_missing} missing · {n_stable} stable",
                    gst_status,
                ]
            )
        finally:
            conn.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(prog="finance", description="Personal CFO skill")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="Sync from Wealthica")
    p_sync.add_argument("--since", default=None, metavar="DATE", help="Start date (YYYY-MM-DD)")
    p_sync.add_argument("--full", action="store_true", help="Full history sync from 2020-01-01")

    p_import = sub.add_parser("import", help="Import CSV transactions")
    p_import.add_argument("file", help="CSV file path")
    p_import.add_argument("--account", default=None, metavar="ID", help="Account ID to assign")
    p_import.add_argument(
        "--owner", choices=["personal", "altaforma"], default="personal", help="Owner to assign"
    )

    p_holdings = sub.add_parser("import-holdings", help="Import Wealthsimple holdings report")
    p_holdings.add_argument("file", help="Holdings CSV path")
    p_holdings.add_argument(
        "--as-of",
        default=None,
        metavar="DATE",
        help="Snapshot date (YYYY-MM-DD), default from filename",
    )

    sub.add_parser("accounts", help="Table of accounts by type with balances")
    sub.add_parser("liquid", help="Net liquid position and buffer-goal gap")

    p_spend = sub.add_parser("spend", help="Spend breakdown by category")
    p_spend.add_argument("--month", action="store_true")
    p_spend.add_argument("--week", action="store_true")
    p_spend.add_argument("--from", dest="from_date", default=None, metavar="DATE")
    p_spend.add_argument("--to", dest="to_date", default=None, metavar="DATE")
    p_spend.add_argument("--owner", choices=["personal", "altaforma"], default=None)
    p_spend.add_argument(
        "--include-transfers",
        action="store_true",
        help="Include internal transfers and loan payments in totals",
    )

    p_top = sub.add_parser("top", help="Top merchants by spend")
    p_top.add_argument("n", nargs="?", type=int, default=10, metavar="N")
    p_top.add_argument("--period", choices=["month", "week"], default="month")

    p_search = sub.add_parser("search", help="Search transactions")
    p_search.add_argument("term")

    p_net = sub.add_parser("net", help="Income vs spend net for period")
    p_net.add_argument("--month", action="store_true")
    p_net.add_argument("--from", dest="from_date", default=None, metavar="DATE")
    p_net.add_argument("--to", dest="to_date", default=None, metavar="DATE")
    p_net.add_argument(
        "--include-transfers",
        action="store_true",
        help="Include internal transfers and loan payments in totals",
    )

    p_compare = sub.add_parser("compare", help="This period vs trailing median per category")
    p_compare.add_argument(
        "--month", action="store_true", help="Compare current calendar month (default)"
    )
    p_compare.add_argument(
        "--week", action="store_true", help="Compare current calendar week (8-week median)"
    )
    p_compare.add_argument("--from", dest="from_date", default=None, metavar="DATE")
    p_compare.add_argument("--to", dest="to_date", default=None, metavar="DATE")
    p_compare.add_argument(
        "--baseline",
        type=int,
        default=3,
        metavar="N",
        help="Number of full periods to use as baseline (default 3 months / 8 weeks)",
    )

    p_runway = sub.add_parser("runway", help="Liquid / monthly obligations in months")
    p_runway.add_argument(
        "--include-inheritance",
        action="store_true",
        help="Add $1,300/mo Ricky inheritance to liquid before dividing",
    )

    p_altaforma = sub.add_parser("altaforma", help="Altaforma spend/revenue by category")
    p_alta_grp = p_altaforma.add_mutually_exclusive_group()
    p_alta_grp.add_argument(
        "--quarter", action="store_true", help="Current quarter (default: year)"
    )
    p_alta_grp.add_argument("--year", action="store_true", help="Current year (default)")

    sub.add_parser("subs-audit", help="Subscription audit: NEW / MISSING / CHANGED / Stable")

    sub.add_parser("recurring", help="Table of recurring charges")
    sub.add_parser("debt", help="Debt stack — balances, monthly payments, projected payoff")
    sub.add_parser("apply-rules", help="Apply classification rules to all existing transactions")

    p_bills = sub.add_parser("bills-due", help="Upcoming obligations vs chequing")
    p_bills.add_argument("--days", type=int, default=7, metavar="N")

    p_tag = sub.add_parser("tag", help="Tag transaction owner/category")
    p_tag.add_argument("txn_id")
    p_tag.add_argument("owner", choices=["personal", "altaforma"])
    p_tag.add_argument("--category", default=None, metavar="CAT")

    p_rule = sub.add_parser("rule", help="Manage classification rules")
    rule_sub = p_rule.add_subparsers(dest="rule_cmd", required=True)

    p_rule_add = rule_sub.add_parser("add", help="Add a new LIKE classification rule")
    p_rule_add.add_argument("pattern", help="SQL LIKE pattern (e.g. %%DIGITALOCEAN%%)")
    p_rule_add.add_argument("owner", choices=["personal", "altaforma"])
    p_rule_add.add_argument("--category", default=None, metavar="CAT")

    rule_sub.add_parser("list", help="List all classification rules")

    p_rule_remove = rule_sub.add_parser("remove", help="Remove a rule by id")
    p_rule_remove.add_argument("id", type=int)

    p_scrape = sub.add_parser("scrape", help="Scrape transactions from bank via Playwright")
    p_scrape.add_argument("--bank", required=True, choices=["rbc", "td"], help="Bank to scrape")
    p_scrape.add_argument("--days", type=int, default=30, metavar="N", help="Days of history")
    p_scrape.add_argument("--first-auth", action="store_true", help="Force headful login + MFA")
    p_scrape.add_argument("--dry-run", action="store_true", help="Print instead of importing")

    args = parser.parse_args()
    conn = init_db(str(DB_PATH))

    try:
        if args.cmd == "sync":
            print(cmd_sync(conn, args.since, args.full))

        elif args.cmd == "import":
            print(cmd_import(conn, args.file, args.account, args.owner))

        elif args.cmd == "import-holdings":
            print(cmd_import_holdings(conn, args.file, args.as_of))

        elif args.cmd == "accounts":
            print(cmd_accounts(conn))

        elif args.cmd == "liquid":
            print(cmd_liquid(conn))

        elif args.cmd == "spend":
            period_start, period_end = _resolve_period(
                args.month, args.week, args.from_date, args.to_date
            )
            print(cmd_spend(conn, period_start, period_end, args.owner, args.include_transfers))

        elif args.cmd == "top":
            if args.period == "week":
                period_start, period_end = _week_range()
            else:
                period_start, period_end = _month_range()
            print(cmd_top(conn, args.n, period_start, period_end))

        elif args.cmd == "search":
            print(cmd_search(conn, args.term))

        elif args.cmd == "net":
            period_start, period_end = _resolve_period(
                args.month, False, args.from_date, args.to_date
            )
            print(cmd_net(conn, period_start, period_end, args.include_transfers))

        elif args.cmd == "compare":
            if args.week:
                period_start, period_end = _week_range()
                baseline_n = args.baseline if args.baseline != 3 else 8
                print(
                    cmd_compare(
                        conn, period_start, period_end, baseline_months=baseline_n, week_mode=True
                    )
                )
            else:
                period_start, period_end = _resolve_period(
                    args.month, False, args.from_date, args.to_date
                )
                print(cmd_compare(conn, period_start, period_end, args.baseline))

        elif args.cmd == "runway":
            print(cmd_runway(conn, include_inheritance=args.include_inheritance))

        elif args.cmd == "altaforma":
            print(cmd_altaforma(conn, quarter=args.quarter))

        elif args.cmd == "subs-audit":
            print(cmd_subs_audit(conn))

        elif args.cmd == "recurring":
            print(cmd_recurring(conn))

        elif args.cmd == "debt":
            print(cmd_debt(conn))

        elif args.cmd == "apply-rules":
            n = _apply_rules_all(conn)
            print(f"Applied rules to all transactions — {n} row(s) updated.")

        elif args.cmd == "bills-due":
            print(cmd_bills_due(conn, args.days))

        elif args.cmd == "tag":
            print(cmd_tag(conn, args.txn_id, args.owner, args.category))

        elif args.cmd == "rule":
            if args.rule_cmd == "add":
                print(cmd_rule_add(conn, args.pattern, args.owner, args.category))
            elif args.rule_cmd == "list":
                print(cmd_rule_list(conn))
            elif args.rule_cmd == "remove":
                print(cmd_rule_remove(conn, args.id))

        elif args.cmd == "scrape":
            print(
                cmd_scrape(
                    conn,
                    bank=args.bank,
                    days=args.days,
                    first_auth=args.first_auth,
                    dry_run=args.dry_run,
                )
            )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
