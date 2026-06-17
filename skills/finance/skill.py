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
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve sibling modules — works when invoked as `python skill.py <cmd>`
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skills.finance.alerts import check_alerts  # noqa: E402
from skills.finance.csv_import import parse_csv  # noqa: E402
from skills.finance.db import (  # noqa: E402
    _normalize_merchant,
    detect_recurring,
    get_accounts,
    get_recurring,
    get_transactions,
    init_db,
    upsert_account,
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


def cmd_import(conn, file_path: str, account_id: str | None = None, owner: str = "personal") -> str:
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    txns = parse_csv(str(path), account_id or "csv-unknown")
    inserted = skipped = 0
    for txn in txns:
        txn["owner"] = owner
        if upsert_transaction(conn, txn):
            inserted += 1
        else:
            skipped += 1

    return f"Imported {inserted} transactions ({skipped} skipped as duplicates)."


# ---------------------------------------------------------------------------
# Command: accounts
# ---------------------------------------------------------------------------


def cmd_accounts(conn) -> str:
    accounts = get_accounts(conn)
    if not accounts:
        return "No accounts. Run `finance sync` first."

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

    net = bank_total - cc_total
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


def cmd_spend(conn, period_start: str, period_end: str, owner: str | None = None) -> str:
    txns = get_transactions(conn, start_date=period_start, end_date=period_end, owner=owner)
    spends: dict[str, float] = {}
    for txn in txns:
        if txn["amount"] >= 0:
            continue
        cat = txn.get("category") or "Uncategorized"
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
        norm = _normalize_merchant(txn.get("description") or "Unknown")
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


def cmd_net(conn, period_start: str, period_end: str) -> str:
    txns = get_transactions(conn, start_date=period_start, end_date=period_end)
    income = sum(t["amount"] for t in txns if t["amount"] > 0)
    spend = sum(abs(t["amount"]) for t in txns if t["amount"] < 0)
    net = income - spend
    net_str = f"+${net:,.2f}" if net >= 0 else f"-${abs(net):,.2f}"
    return (
        f"**Net: {period_start} → {period_end}**\n"
        f"  In:  ${income:,.2f}\n"
        f"  Out: ${spend:,.2f}\n"
        f"  Net: {net_str}"
    )


# ---------------------------------------------------------------------------
# Command: recurring
# ---------------------------------------------------------------------------


def cmd_recurring(conn) -> str:
    rows = get_recurring(conn)
    if not rows:
        return "No recurring charges detected. Run `finance sync` to analyse."

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
            return await scraper_rbc.fetch_transactions(days=days)

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

    sub.add_parser("accounts", help="Table of accounts by type with balances")
    sub.add_parser("liquid", help="Net liquid position and buffer-goal gap")

    p_spend = sub.add_parser("spend", help="Spend breakdown by category")
    p_spend.add_argument("--month", action="store_true")
    p_spend.add_argument("--week", action="store_true")
    p_spend.add_argument("--from", dest="from_date", default=None, metavar="DATE")
    p_spend.add_argument("--to", dest="to_date", default=None, metavar="DATE")
    p_spend.add_argument("--owner", choices=["personal", "altaforma"], default=None)

    p_top = sub.add_parser("top", help="Top merchants by spend")
    p_top.add_argument("n", nargs="?", type=int, default=10, metavar="N")
    p_top.add_argument("--period", choices=["month", "week"], default="month")

    p_search = sub.add_parser("search", help="Search transactions")
    p_search.add_argument("term")

    p_net = sub.add_parser("net", help="Income vs spend net for period")
    p_net.add_argument("--month", action="store_true")
    p_net.add_argument("--from", dest="from_date", default=None, metavar="DATE")
    p_net.add_argument("--to", dest="to_date", default=None, metavar="DATE")

    sub.add_parser("recurring", help="Table of recurring charges")

    p_bills = sub.add_parser("bills-due", help="Upcoming obligations vs chequing")
    p_bills.add_argument("--days", type=int, default=7, metavar="N")

    p_tag = sub.add_parser("tag", help="Tag transaction owner/category")
    p_tag.add_argument("txn_id")
    p_tag.add_argument("owner", choices=["personal", "altaforma"])
    p_tag.add_argument("--category", default=None, metavar="CAT")

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

        elif args.cmd == "accounts":
            print(cmd_accounts(conn))

        elif args.cmd == "liquid":
            print(cmd_liquid(conn))

        elif args.cmd == "spend":
            period_start, period_end = _resolve_period(
                args.month, args.week, args.from_date, args.to_date
            )
            print(cmd_spend(conn, period_start, period_end, args.owner))

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
            print(cmd_net(conn, period_start, period_end))

        elif args.cmd == "recurring":
            print(cmd_recurring(conn))

        elif args.cmd == "bills-due":
            print(cmd_bills_due(conn, args.days))

        elif args.cmd == "tag":
            print(cmd_tag(conn, args.txn_id, args.owner, args.category))

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
