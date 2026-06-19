"""CSV importer for RBC, MBNA, and Rogers credit card statements.

Dedup key: SHA256(date + str(amount) + description) so the same transaction
imported twice produces the same id and is silently skipped by upsert_transaction.

Amount sign convention (matches db.py):
  negative  = money out (purchases, fees)
  positive  = money in (payments, refunds, credits)

RBC CSV: CAD$ column already uses our sign convention (negative = spend).
MBNA CSV: purchases are positive in the export → we negate them.
Rogers CSV: separate Credit/Debit columns → amount = credit - debit.
"""

from __future__ import annotations

import csv
import hashlib
import re
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def parse_holdings(path: str) -> list[dict]:
    """Parse a Wealthsimple holdings report CSV.

    Returns list of position dicts ready for upsert_position.
    Also returns per-account market_value_cad totals so cmd_import can
    update balance_current on investment accounts.

    The file has a trailing "As of..." line which csv.DictReader ignores
    (it doesn't match the header column count).

    market_value_cad: for CAD-priced positions, Market Value is already CAD.
    For USD-priced positions, Book Value (CAD) gives the cost in CAD but not
    the current market value in CAD. We use quantity × market_price as the
    native value and store it; for the CAD equivalent we use the ratio
    market_value_native / book_value_native × book_value_cad as an approximation
    when a direct CAD market value isn't available.
    Actually: for USD positions the file has no Market Value (CAD) column, but
    Book Value (CAD) ÷ Book Value (Market) gives us the implicit FX rate at
    cost. We use that same rate to convert current market value to CAD.
    """
    positions: list[dict] = []
    # Account code → WS account ID mapping (same as activities files)
    _WS_ACCOUNT_MAP = {
        "WK19SSZN2CAD": "ws-WK19SSZN2CAD",
        "WK2P9ZT49CAD": "ws-WK2P9ZT49CAD",
        "WK3QRZ8Q0CAD": "ws-WK3QRZ8Q0CAD",
        "WK40VYS33CAD": "ws-WK40VYS33CAD",
        "HQ6YWF114CAD": "ws-HQ6YWF114CAD",
    }

    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Skip the trailing "As of..." summary line
            if not row.get("Symbol") or not row.get("Account Number"):
                continue
            try:
                acct_code = row["Account Number"].strip()
                account_id = _WS_ACCOUNT_MAP.get(acct_code, f"ws-{acct_code}")
                symbol = row["Symbol"].strip()
                name = row["Name"].strip()
                quantity = _clean_amount(row["Quantity"])
                market_price = _clean_amount(row["Market Price"])
                price_currency = row["Market Price Currency"].strip()
                market_value_native = quantity * market_price

                book_value_cad = _clean_amount(row["Book Value (CAD)"])
                book_value_native = _clean_amount(row["Book Value (Market)"])

                # Derive CAD market value
                if price_currency == "CAD":
                    market_value_cad = market_value_native
                else:
                    # Use implicit FX rate from book values if available
                    if book_value_native and book_value_native != 0:
                        fx_rate = book_value_cad / book_value_native
                    else:
                        fx_rate = 1.0
                    market_value_cad = market_value_native * fx_rate

                unrealized_cad = market_value_cad - book_value_cad

                positions.append(
                    {
                        "id": f"{account_id}:{symbol}",
                        "account_id": account_id,
                        "symbol": symbol,
                        "name": name,
                        "quantity": quantity,
                        "market_price": market_price,
                        "price_currency": price_currency,
                        "market_value_native": market_value_native,
                        "market_value_cad": market_value_cad,
                        "book_value_cad": book_value_cad,
                        "unrealized_cad": unrealized_cad,
                        "as_of": None,  # filled in by caller from filename or today
                    }
                )
            except (ValueError, KeyError):
                continue

    return positions


def parse_holdings_balances(positions: list[dict]) -> dict[str, float]:
    """Sum market_value_cad per account from a parsed positions list."""
    totals: dict[str, float] = {}
    for p in positions:
        aid = p["account_id"]
        totals[aid] = totals.get(aid, 0.0) + p["market_value_cad"]
    return totals


def detect_format(path: str) -> str:
    """Return format string based on CSV header row."""
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        first_row = []
        for row in reader:
            if any(cell.strip() for cell in row):
                first_row = row
                break
        if not first_row:
            return "generic"

    headers = [h.strip().lower() for h in first_row]

    if "account type" in headers and "cad$" in headers:
        return "rbc"
    if "posted date" in headers and "payee" in headers and "amount" in headers:
        return "mbna_new"
    if "transaction" in headers and "memo" in headers and "name" in headers:
        return "mbna"
    if "post date" in headers and ("credit" in headers or "debit" in headers):
        return "rogers"
    if {"date", "transaction", "description", "amount", "balance", "currency"}.issubset(
        set(headers)
    ):
        return "wealthsimple"

    # Desjardins: no header, 14 columns, account type in col[2] (PCA or LN1)
    if len(first_row) >= 14 and first_row[2].strip() in ("PCA", "LN1"):
        return "desjardins"

    # TD: no header row — 5 columns: date, description, debit, credit, balance
    # CC uses MM/DD/YYYY; chequing/savings uses YYYY-MM-DD
    if len(first_row) == 5 and not headers[0].startswith("date"):
        from datetime import datetime

        date_val = first_row[0].strip().strip('"')
        for fmt, kind in (("%m/%d/%Y", "td_cc"), ("%Y-%m-%d", "td_bank")):
            try:
                datetime.strptime(date_val, fmt)
                return kind
            except ValueError:
                pass

    return "generic"


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


def parse_csv(path: str, account_id: str | None = None) -> list[dict]:
    """Parse CSV file → list of transaction dicts ready for upsert_transaction.

    For RBC files, account_id is derived from the Account Number column if not
    provided explicitly. For TD CC files, account_id must be supplied via
    --account flag since the file has no account number.
    """
    fmt = detect_format(path)
    if fmt == "rbc":
        return _parse_rbc(path, account_id)
    if fmt == "td_cc":
        return _parse_td_cc(path, account_id or "td-cc-unknown")
    if fmt == "td_bank":
        return _parse_td_bank(path, account_id or "td-bank-unknown")
    if fmt == "desjardins":
        return _parse_desjardins(path, account_id)
    if fmt == "wealthsimple":
        return _parse_wealthsimple(path, account_id)
    if fmt == "mbna_new":
        return _parse_mbna_new(path, account_id or "mbna-unknown")
    if fmt == "mbna":
        return _parse_mbna(path, account_id or "mbna-unknown")
    if fmt == "rogers":
        return _parse_rogers(path, account_id or "rogers-unknown")
    return _parse_generic(path, account_id or "unknown")


# ---------------------------------------------------------------------------
# Balance extractor
# ---------------------------------------------------------------------------


def parse_csv_balances(path: str, account_id: str | None = None) -> dict[str, float]:
    """Return {account_id: latest_balance} for each account found in the file.

    Picks the row with the latest date per account so the result is correct
    regardless of whether the bank exports oldest-first or newest-first.
    Returns empty dict if the format has no balance column.
    """
    fmt = detect_format(path)
    # {aid: (date_str, balance)} — date_str kept sortable (ISO or padded)
    best: dict[str, tuple[str, float]] = {}

    if fmt == "rbc":
        return {}

    if fmt in ("td_cc", "td_bank"):
        aid = account_id or ("td-cc-unknown" if fmt == "td_cc" else "td-bank-unknown")
        date_fmt = "%m/%d/%Y" if fmt == "td_cc" else "%Y-%m-%d"
        with Path(path).open(newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if len(row) < 5:
                    continue
                try:
                    date_str = datetime.strptime(row[0].strip().strip('"'), date_fmt).strftime(
                        "%Y-%m-%d"
                    )
                    bal = _clean_amount(row[4])
                    if aid not in best or date_str >= best[aid][0]:
                        best[aid] = (date_str, bal)
                except ValueError:
                    pass
        return {aid: v[1] for aid, v in best.items()}

    if fmt == "mbna_new":
        return {}

    if fmt == "desjardins":
        with Path(path).open(newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if len(row) < 14:
                    continue
                try:
                    acct_type = row[2].strip()
                    member_id = row[1].strip()
                    # Always derive per-account keys from the row — an explicit
                    # account_id would collapse PCA+LN1 onto the same key and
                    # lose one account's balance.
                    if acct_type == "PCA":
                        aid = f"dsj-pca-{member_id}"
                    elif acct_type == "LN1":
                        aid = f"dsj-ln1-{member_id}"
                    else:
                        continue
                    date_str = row[3].strip()
                    bal = _clean_amount(row[13])
                    if aid not in best or date_str >= best[aid][0]:
                        best[aid] = (date_str, bal)
                except ValueError:
                    pass
        return {aid: v[1] for aid, v in best.items()}

    if fmt == "wealthsimple":
        filename_stem = Path(path).stem
        parts = filename_stem.split("-")
        code_part = parts[-1] if parts else "unknown"
        aid = account_id or f"ws-{code_part}"
        with Path(path).open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                currency = (row.get("currency") or "CAD").strip().upper()
                if currency != "CAD":
                    continue
                try:
                    date_str = (row.get("date") or "").strip()
                    bal = _clean_amount(row.get("balance", "0"))
                    if aid not in best or date_str >= best[aid][0]:
                        best[aid] = (date_str, bal)
                except ValueError:
                    pass
        return {aid: v[1] for aid, v in best.items()}

    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Patterns for RBC Interac transactions — all four forms encode (terminal_code, merchant).
# We canonicalize so the same physical purchase hashes identically regardless of form.
#   INTERAC PURCHASE - {CODE} {MERCHANT}
#   CONTACTLESS INTERAC PURCHASE - {CODE} {MERCHANT}
#   C-IDP PURCHASE-{CODE} {MERCHANT}
#   {MERCHANT} IDP PURCHASE - {CODE}
_RBC_INTERAC_PATTERNS = [
    re.compile(r"^(?:CONTACTLESS )?INTERAC PURCHASE - (\d{4}) (.+)$"),
    re.compile(r"^C-IDP PURCHASE-(\d{4}) (.+)$"),
    re.compile(r"^(.+?) IDP PURCHASE - (\d{4})$"),
]

# Patterns for RBC online payment transactions — both forms describe the same payment.
#   Payment WWW PAYMENT - {CODE} {PAYEE}
#   ONLINE BANKING PAYMENT - {CODE} {PAYEE}
_RBC_PAYMENT_PATTERNS = [
    re.compile(r"^Payment WWW PAYMENT - (\d{4}) (.+)$"),
    re.compile(r"^ONLINE BANKING PAYMENT - (\d{4}) (.+)$"),
]

# Patterns for RBC online transfers — both forms describe the same transfer.
#   ONLINE BANKING TRANSFER - {CODE}
#   Transfer WWW TRANSFER - {CODE}
_RBC_TRANSFER_PATTERNS = [
    re.compile(r"^ONLINE BANKING TRANSFER - (\d{4})$"),
    re.compile(r"^Transfer WWW TRANSFER - (\d{4})$"),
]

# RBC e-transfer shadow rows — "Email Trfs E-TRANSFER SENT/RECEIVED" is RBC's generic
# description for the same transaction that also appears as the named form
# "E-TRANSFER SENT/RECEIVED {NAME} {CODE}". The shadow row has the same date/amount/account.
# We canonicalize shadow rows to match the named form's dedup key so they hash identically.
_RBC_ETRANSFER_SHADOW_PATTERN = re.compile(r"^Email Trfs (E-TRANSFER (?:SENT|RECEIVED))$")


def _rbc_interac_canonical(description: str) -> str:
    """Return a stable canonical string for RBC Interac purchase descriptions.

    All four variants of the same terminal transaction reduce to 'MERCHANT [CODE]'.
    Returns the original description unchanged if it doesn't match any known pattern.
    """
    for pat in _RBC_INTERAC_PATTERNS:
        m = pat.match(description)
        if m:
            if pat.pattern.endswith(r"(\d{4})$"):
                # {MERCHANT} IDP PURCHASE - {CODE}  →  groups are (merchant, code)
                merchant, code = m.group(1).strip(), m.group(2)
            else:
                # code-first patterns  →  groups are (code, merchant)
                code, merchant = m.group(1), m.group(2).strip()
            return f"{merchant} [{code}]"
    return description


def _rbc_payment_canonical(description: str) -> str:
    """Return a stable canonical string for RBC online payment descriptions.

    Both 'Payment WWW PAYMENT - {CODE} {PAYEE}' and
    'ONLINE BANKING PAYMENT - {CODE} {PAYEE}' reduce to 'PAYMENT {PAYEE} [PMT-{CODE}]'.
    Returns the original description unchanged if it doesn't match.
    """
    for pat in _RBC_PAYMENT_PATTERNS:
        m = pat.match(description)
        if m:
            code, payee = m.group(1), m.group(2).strip()
            return f"PAYMENT {payee} [PMT-{code}]"
    return description


def _dedup_key(date_str: str, amount: float, description: str) -> str:
    raw = f"{date_str}{amount:.2f}{description}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _rbc_transfer_canonical(description: str) -> str:
    """Normalize 'ONLINE BANKING TRANSFER - {CODE}' and 'Transfer WWW TRANSFER - {CODE}'.

    Both forms describe the same RBC online transfer — canonicalize to
    'TRANSFER [{CODE}]' so they hash identically.
    """
    for pat in _RBC_TRANSFER_PATTERNS:
        m = pat.match(description)
        if m:
            return f"TRANSFER [{m.group(1)}]"
    return description


def _rbc_dedup_key(date_str: str, amount: float, description: str) -> str:
    """Dedup key for RBC transactions — canonicalizes all known duplicate description forms.

    Handles: Interac purchase variants, payment variants (WWW/ONLINE BANKING),
    transfer variants (WWW/ONLINE BANKING), and payroll case variants.
    Shadow 'Email Trfs' rows are deleted post-import by the cleanup script.
    """
    canonical = _rbc_interac_canonical(description)
    canonical = _rbc_payment_canonical(canonical)
    canonical = _rbc_transfer_canonical(canonical)
    # Payroll descriptions are case-inconsistent across CSV exports — uppercase to normalize.
    if canonical.upper().startswith("PAYROLL DEPOSIT"):
        canonical = canonical.upper()
    raw = f"{date_str}{amount:.2f}{canonical}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_date(s: str) -> str | None:
    """Normalise date string to YYYY-MM-DD; returns None on parse failure."""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _clean_amount(s: str) -> float:
    """Strip currency formatting and convert to float."""
    return float(s.strip().replace('"', "").replace(",", "").replace("$", "") or "0")


# ---------------------------------------------------------------------------
# Format-specific parsers
# ---------------------------------------------------------------------------


def _parse_rbc(path: str, account_id: str | None) -> list[dict]:
    """RBC format: Account Type, Account Number, Transaction Date, Cheque Number,
    Description 1, Description 2, CAD$, USD$

    Sign convention is already correct: negative = spend, positive = income.
    account_id defaults to 'rbc-<AccountNumber>' if not supplied.
    """
    txns: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                date_str = _parse_date(row.get("Transaction Date", ""))
                if not date_str:
                    continue

                cad = (row.get("CAD$") or "").strip()
                if not cad:
                    continue
                amount = _clean_amount(cad)

                desc1 = (row.get("Description 1") or "").strip()
                desc2 = (row.get("Description 2") or "").strip()

                # LOC/loan accounts encode the charge amount in Description 2
                # when CAD$ is 0.00 (e.g. interest, insurance premiums).
                if amount == 0.0 and desc2:
                    try:
                        amount = -abs(_clean_amount(desc2))
                        description = desc1
                    except ValueError:
                        description = f"{desc1} {desc2}".strip()
                else:
                    description = f"{desc1} {desc2}".strip() if desc2 else desc1
                if not description:
                    continue

                acct_num = (row.get("Account Number") or "").strip().replace(" ", "")
                derived_id = account_id or f"rbc-{acct_num}"

                txns.append(
                    {
                        "id": _rbc_dedup_key(date_str, amount, description),
                        "account_id": derived_id,
                        "date": date_str,
                        "amount": amount,
                        "description": description,
                        "category": None,
                        "currency": "CAD",
                        "is_pending": 0,
                        "source": "csv_import_rbc",
                    }
                )
            except (ValueError, KeyError):
                continue
    return txns


def _parse_td_cc(path: str, account_id: str) -> list[dict]:
    """TD credit card format: no header row.

    Columns: date (MM/DD/YYYY), description, charge, payment, balance
    charge populated = spend → negative; payment populated = credit to card → positive.
    """
    txns: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 4:
                continue
            try:
                date_str = _parse_date(row[0])
                if not date_str:
                    continue
                description = row[1].strip()
                if not description:
                    continue
                charge = _clean_amount(row[2]) if row[2].strip() else 0.0
                payment = _clean_amount(row[3]) if row[3].strip() else 0.0
                amount = payment - charge  # payment=positive (money in), charge=negative (spend)
                txns.append(
                    {
                        "id": _dedup_key(date_str, amount, description),
                        "account_id": account_id,
                        "date": date_str,
                        "amount": amount,
                        "description": description,
                        "category": None,
                        "currency": "CAD",
                        "is_pending": 0,
                        "source": "csv_import_td_cc",
                    }
                )
            except (ValueError, IndexError):
                continue
    return txns


def _parse_td_bank(path: str, account_id: str) -> list[dict]:
    """TD chequing/savings format: no header row, values quoted.

    Columns: date (YYYY-MM-DD), description, debit, credit, balance
    debit populated = money out (negative); credit populated = money in (positive).
    """
    txns: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 4:
                continue
            try:
                date_str = _parse_date(row[0].strip())
                if not date_str:
                    continue
                description = row[1].strip()
                if not description:
                    continue
                debit = _clean_amount(row[2]) if row[2].strip() else 0.0
                credit = _clean_amount(row[3]) if row[3].strip() else 0.0
                amount = credit - debit
                txns.append(
                    {
                        "id": _dedup_key(date_str, amount, description),
                        "account_id": account_id,
                        "date": date_str,
                        "amount": amount,
                        "description": description,
                        "category": None,
                        "currency": "CAD",
                        "is_pending": 0,
                        "source": "csv_import_td_bank",
                    }
                )
            except (ValueError, IndexError):
                continue
    return txns


def _parse_desjardins(path: str, account_id: str | None) -> list[dict]:
    """Desjardins CSV: no header, 14 columns.

    col[2] = account type (PCA = chequing, LN1 = mortgage)
    col[3] = date (YYYY/MM/DD)
    col[5] = description
    col[7] = withdrawal (PCA debit)
    col[8] = deposit (PCA credit)
    col[9] = interest (LN1)
    col[12] = payment amount (LN1)

    PCA: account_id defaults to 'dsj-pca-710490'
    LN1: stored as two rows per payment — interest (negative) + principal (negative)
         account_id defaults to 'dsj-ln1-710490'
    """
    txns: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 14:
                continue
            try:
                acct_type = row[2].strip()
                member_id = row[1].strip()
                date_str = _parse_date(row[3].strip().replace("/", "-"))
                if not date_str:
                    continue
                description = row[5].strip()
                if not description:
                    continue

                if acct_type == "PCA":
                    acct_id = account_id or f"dsj-pca-{member_id}"
                    withdrawal = _clean_amount(row[7]) if row[7].strip() else 0.0
                    deposit = _clean_amount(row[8]) if row[8].strip() else 0.0
                    amount = deposit - withdrawal
                    txns.append(
                        {
                            "id": _dedup_key(date_str, amount, description),
                            "account_id": acct_id,
                            "date": date_str,
                            "amount": amount,
                            "description": description,
                            "category": None,
                            "currency": "CAD",
                            "is_pending": 0,
                            "source": "csv_import_desjardins",
                        }
                    )

                elif acct_type == "LN1":
                    acct_id = account_id or f"dsj-ln1-{member_id}"
                    interest = _clean_amount(row[9]) if row[9].strip() else 0.0
                    principal = _clean_amount(row[12]) if row[12].strip() else 0.0
                    if principal == 0.0 and interest == 0.0:
                        continue
                    total = -(interest + principal)
                    desc = f"{description} (interest: ${interest:.2f}, principal: ${principal:.2f}, total: ${interest+principal:.2f})"
                    txns.append(
                        {
                            "id": _dedup_key(date_str, total, description),
                            "account_id": acct_id,
                            "date": date_str,
                            "amount": total,
                            "description": desc,
                            "category": "mortgage",
                            "currency": "CAD",
                            "is_pending": 0,
                            "source": "csv_import_desjardins",
                        }
                    )

            except (ValueError, IndexError):
                continue
    return txns


def _parse_wealthsimple(path: str, account_id: str | None) -> list[dict]:
    """Wealthsimple monthly statement format: date, transaction, description, amount, balance, currency.

    Amount sign is already correct (BUY/FEE negative, SELL/DIV/CONT/GRANT positive).
    account_id is derived from the filename's account code (e.g. 'WK19SSZN2CAD') if not supplied.
    Non-CAD rows (e.g. crypto in BTC) are skipped — amounts are not comparable without FX.
    """
    txns: list[dict] = []

    # Derive account ID from filename: the segment before .csv after the last '-'
    # e.g. "BDL Pension-2026-03-01-monthly-statement-transactions-WK19SSZN2CAD.csv"
    #   → "ws-WK19SSZN2CAD"
    filename_stem = Path(path).stem
    parts = filename_stem.split("-")
    # Last part is the account code (all-caps alphanumeric)
    code_part = parts[-1] if parts else "unknown"
    derived_id = account_id or f"ws-{code_part}"

    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                currency = (row.get("currency") or "CAD").strip().upper()
                if currency != "CAD":
                    continue  # skip crypto/USD rows — no reliable CAD conversion in the file

                date_str = _parse_date(row.get("date", ""))
                if not date_str:
                    continue

                txn_type = (row.get("transaction") or "").strip().upper()
                description = (row.get("description") or "").strip()
                if not description:
                    continue

                amount = _clean_amount(row.get("amount", "0"))

                txns.append(
                    {
                        "id": _dedup_key(date_str, amount, description),
                        "account_id": derived_id,
                        "date": date_str,
                        "amount": amount,
                        "description": description,
                        "category": _ws_category(txn_type),
                        "currency": currency,
                        "is_pending": 0,
                        "source": "csv_import_wealthsimple",
                    }
                )
            except (ValueError, KeyError):
                continue
    return txns


def _ws_category(txn_type: str) -> str | None:
    return {
        "DIV": "investment_income",
        "INT": "investment_income",
        "FEE": "investment_fee",
        "BUY": "investment_buy",
        "SELL": "investment_sell",
        "CONT": "investment_contribution",
        "GRANT": "investment_grant",
        "REIMB": "investment_rebate",
    }.get(txn_type)


def _parse_mbna_new(path: str, account_id: str) -> list[dict]:
    """MBNA format: Posted Date, Payee, Address, Amount.

    Amount sign already correct: negative = spend, positive = payment/refund.
    """
    txns: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                date_str = _parse_date(row.get("Posted Date", ""))
                if not date_str:
                    continue
                description = (row.get("Payee") or "").strip()
                if not description:
                    continue
                amount = _clean_amount(row.get("Amount", "0"))
                txns.append(
                    {
                        "id": _dedup_key(date_str, amount, description),
                        "account_id": account_id,
                        "date": date_str,
                        "amount": amount,
                        "description": description,
                        "category": None,
                        "currency": "CAD",
                        "is_pending": 0,
                        "source": "csv_import_mbna",
                    }
                )
            except (ValueError, KeyError):
                continue
    return txns


def _parse_mbna(path: str, account_id: str) -> list[dict]:
    """MBNA format: Date, Transaction, Name, Memo, Amount.

    MBNA exports purchases as positive → we negate to match our convention.
    """
    txns: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                date_str = _parse_date(row.get("Date", ""))
                if not date_str:
                    continue
                raw = row.get("Amount", "0")
                amount = -_clean_amount(raw)  # flip: MBNA positive = spend → our negative
                description = (row.get("Name") or row.get("Transaction") or "").strip()
                if not description:
                    continue
                txns.append(
                    {
                        "id": _dedup_key(date_str, amount, description),
                        "account_id": account_id,
                        "date": date_str,
                        "amount": amount,
                        "description": description,
                        "category": None,
                        "currency": "CAD",
                        "is_pending": 0,
                        "source": "csv_import",
                    }
                )
            except (ValueError, KeyError):
                continue
    return txns


def _parse_rogers(path: str, account_id: str) -> list[dict]:
    """Rogers format: Transaction Date, Post Date, Description, Category, Card Number, Credit, Debit."""
    txns: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                date_str = _parse_date(row.get("Transaction Date", ""))
                if not date_str:
                    continue
                credit = _clean_amount(row.get("Credit") or "0")
                debit = _clean_amount(row.get("Debit") or "0")
                amount = credit - debit  # credit=positive (money in), debit=negative (spend)
                description = (row.get("Description") or "").strip()
                if not description:
                    continue
                category = (row.get("Category") or "").strip() or None
                txns.append(
                    {
                        "id": _dedup_key(date_str, amount, description),
                        "account_id": account_id,
                        "date": date_str,
                        "amount": amount,
                        "description": description,
                        "category": category,
                        "currency": "CAD",
                        "is_pending": 0,
                        "source": "csv_import",
                    }
                )
            except (ValueError, KeyError):
                continue
    return txns


def _parse_generic(path: str, account_id: str) -> list[dict]:
    """Best-effort parser for unknown CSV formats.

    Looks for columns named date/amount/description (case-insensitive).
    """
    txns: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return txns
        headers_lower = {h.strip().lower(): h for h in reader.fieldnames}

        date_col = next((headers_lower[k] for k in headers_lower if "date" in k), None)
        amount_col = next((headers_lower[k] for k in headers_lower if "amount" in k), None)
        desc_col = next(
            (
                headers_lower[k]
                for k in headers_lower
                if k in ("description", "name", "memo", "payee")
            ),
            None,
        )

        if not (date_col and amount_col):
            return txns

        for row in reader:
            try:
                date_str = _parse_date(row.get(date_col, ""))
                if not date_str:
                    continue
                amount = _clean_amount(row.get(amount_col, "0"))
                description = (row.get(desc_col, "") if desc_col else "").strip() or "Unknown"
                txns.append(
                    {
                        "id": _dedup_key(date_str, amount, description),
                        "account_id": account_id,
                        "date": date_str,
                        "amount": amount,
                        "description": description,
                        "category": None,
                        "currency": "CAD",
                        "is_pending": 0,
                        "source": "csv_import",
                    }
                )
            except (ValueError, KeyError):
                continue
    return txns
