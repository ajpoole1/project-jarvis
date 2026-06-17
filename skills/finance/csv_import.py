"""CSV importer for MBNA and Rogers credit card statements.

Dedup key: SHA256(date + str(amount) + description) so the same transaction
imported twice produces the same id and is silently skipped by upsert_transaction.

Amount sign convention (matches db.py):
  negative  = money out (purchases, fees)
  positive  = money in (payments, refunds, credits)

MBNA CSV: purchases are positive in the export → we negate them.
Rogers CSV: separate Credit/Debit columns → amount = credit - debit.
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect_format(path: str) -> str:
    """Return 'mbna', 'rogers', or 'generic' based on CSV header row."""
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            headers = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return "generic"

    if "transaction" in headers and "memo" in headers and "name" in headers:
        return "mbna"
    if "post date" in headers and ("credit" in headers or "debit" in headers):
        return "rogers"
    return "generic"


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


def parse_csv(path: str, account_id: str) -> list[dict]:
    """Parse CSV file → list of transaction dicts ready for upsert_transaction."""
    fmt = detect_format(path)
    if fmt == "mbna":
        return _parse_mbna(path, account_id)
    if fmt == "rogers":
        return _parse_rogers(path, account_id)
    return _parse_generic(path, account_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dedup_key(date_str: str, amount: float, description: str) -> str:
    raw = f"{date_str}{amount:.2f}{description}"
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
