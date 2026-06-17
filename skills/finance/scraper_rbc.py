"""RBC transaction scraper.

Approach: network response interception.
After session restore we navigate to the RBC account summary page. RBC's SPA
loads transactions via an internal REST API at a URL containing
'transaction-presentation-service' or 'getTransactions'. We intercept those
responses using page.on('response') and collect the JSON payload.

Fallback: if no intercept payload arrives within the wait window, we attempt
the CSV export flow (Download / Export button → captured download file).

Amount sign convention (matches finance.db):
  negative = money out (debit / purchase)
  positive = money in  (deposit / credit)
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date, timedelta

from skills.finance.scraper_base import ensure_session, is_auth_expired, save_session
from skills.finance.scraper_errors import ScraperError, SessionExpiredError

_RBC_LOGIN_URL = (
    "https://www.rbcroyalbank.com/cgi-bin/rbaccess/rbunxcgi"
    "?F6=1&F7=IB&F21=IB&F22=IB&REQUEST=ClientSignin&LANGUAGE=ENGLISH"
)
_RBC_SUMMARY_URL = (
    "https://www1.royalbank.com/cgi-bin/rbaccess/rbunxcgi"
    "?F6=1&F7=IB&F21=IB&F22=IB&REQUEST=AccountSummary&LANGUAGE=ENGLISH"
)

_INTERCEPT_KEYWORDS = ("transaction-presentation-service", "gettransactions", "transactions/v")
_RESPONSE_WAIT_S = 20


def _txn_id(date_str: str, amount: float, description: str) -> str:
    raw = f"rbc:{date_str}{amount:.2f}{description}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _normalize_amount(raw: float, txn_type: str | None) -> float:
    """RBC API amounts vary by endpoint — normalise to our sign convention.

    If txn_type is 'debit' or amount > 0 and it represents spend, negate.
    We inspect the 'type' field if present; otherwise trust the sign as-is.
    """
    if txn_type and txn_type.lower() in ("debit", "dr"):
        return -abs(raw)
    if txn_type and txn_type.lower() in ("credit", "cr"):
        return abs(raw)
    return raw


def _extract_transactions_from_payload(payload: dict | list, days: int) -> list[dict]:
    """Walk common RBC API response shapes and extract transaction rows."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    results: list[dict] = []

    # Unwrap common envelope shapes
    if isinstance(payload, dict):
        for key in ("transactions", "transactionList", "data", "items", "result"):
            if key in payload:
                payload = payload[key]
                break

    if not isinstance(payload, list):
        return results

    for item in payload:
        if not isinstance(item, dict):
            continue

        raw_date = item.get("transactionDate") or item.get("date") or item.get("postedDate") or ""
        if raw_date:
            raw_date = raw_date[:10]  # truncate to YYYY-MM-DD

        if raw_date < cutoff:
            continue

        raw_amount = float(
            item.get("amount", 0) or item.get("transactionAmount", 0) or item.get("value", 0) or 0
        )
        txn_type = item.get("type") or item.get("transactionType") or item.get("debitCredit")
        amount = _normalize_amount(raw_amount, txn_type)

        description = (
            item.get("description")
            or item.get("merchantName")
            or item.get("memo")
            or item.get("name")
            or "RBC Transaction"
        ).strip()

        account = (
            item.get("accountNumber")
            or item.get("accountId")
            or item.get("account")
            or "rbc-unknown"
        )

        results.append(
            {
                "id": _txn_id(raw_date, amount, description),
                "date": raw_date,
                "amount": amount,
                "description": description,
                "account": account,
                "account_id": f"rbc-{account}",
                "category": item.get("category"),
                "currency": item.get("currency", "CAD"),
                "is_pending": 0,
                "source": "scraper_rbc",
            }
        )

    return results


async def fetch_transactions(days: int = 30, force_headful: bool = False) -> list[dict]:
    """Fetch RBC transactions for the last `days` days.

    Returns list of dicts compatible with finance.db upsert_transaction schema.
    Raises SessionExpiredError if auth has lapsed.
    Raises ScraperError on unrecoverable failure.
    """
    pw, context = await ensure_session("rbc", force_headful=force_headful)

    try:
        page = await context.new_page()

        # Intercept transaction API responses
        intercepted: list[dict] = []
        intercept_event = asyncio.Event()

        async def _on_response(response):
            url_lower = response.url.lower()
            if any(kw in url_lower for kw in _INTERCEPT_KEYWORDS):
                try:
                    if "application/json" in (response.headers.get("content-type", "")):
                        body = await response.json()
                        txns = _extract_transactions_from_payload(body, days)
                        if txns:
                            intercepted.extend(txns)
                            intercept_event.set()
                except Exception:
                    pass

        page.on("response", _on_response)

        await page.goto(_RBC_SUMMARY_URL, wait_until="domcontentloaded", timeout=30_000)

        if await is_auth_expired(page):
            raise SessionExpiredError(
                "RBC session expired — run: finance scrape --bank rbc --first-auth"
            )

        # Wait for intercept to fire or timeout, then settle for concurrent account requests
        try:
            await asyncio.wait_for(intercept_event.wait(), timeout=_RESPONSE_WAIT_S)
            await asyncio.sleep(3.0)
        except TimeoutError:
            pass

        if not intercepted:
            # Fallback: attempt CSV export
            intercepted = await _csv_export_fallback(page, days)

        if not intercepted:
            raise ScraperError(
                "RBC: no transaction data retrieved. "
                "The page structure may have changed — inspect manually."
            )

        await save_session("rbc", context)
        return intercepted

    finally:
        await context.close()
        await pw.stop()


async def _csv_export_fallback(page, days: int) -> list[dict]:
    """Attempt to trigger RBC's CSV export and parse the downloaded file."""
    import csv
    import io
    from datetime import datetime

    cutoff = (date.today() - timedelta(days=days)).isoformat()

    try:
        # RBC may have an "Export" or "Download" link — try common selectors
        export_selectors = [
            "a[href*='export']",
            "button:has-text('Export')",
            "a:has-text('Download')",
            "button:has-text('Download')",
            "[data-testid*='export']",
            "[data-testid*='download']",
        ]

        async with page.expect_download(timeout=15_000) as download_info:
            clicked = False
            for sel in export_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click(timeout=3_000)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                return []

        download = await download_info.value
        content = await download.path()
        if content is None:
            return []

        with open(content, newline="", encoding="utf-8-sig") as f:
            text = f.read()

        results: list[dict] = []
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            raw_date = ""
            for col in ("Date", "Transaction Date", "Post Date"):
                if row.get(col):
                    raw_date = row[col].strip()
                    break
            if not raw_date:
                continue

            # Normalise date
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    raw_date = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue

            if raw_date < cutoff:
                continue

            description = (row.get("Description") or row.get("Memo") or "").strip()

            def _parse_amount(val: str | None) -> float:
                return float((val or "0").replace(",", "").replace("$", "").strip() or "0")

            amount_col = row.get("Amount")
            if amount_col is not None:
                amount = _parse_amount(amount_col)
            else:
                debit = _parse_amount(row.get("Debit"))
                credit = _parse_amount(row.get("Credit"))
                amount = credit - debit  # credit=positive, debit=negative

            account = (row.get("Account Number") or row.get("Account") or "rbc-csv").strip()

            results.append(
                {
                    "id": _txn_id(raw_date, amount, description),
                    "date": raw_date,
                    "amount": amount,
                    "description": description,
                    "account": account,
                    "account_id": f"rbc-{account}",
                    "category": row.get("Category"),
                    "currency": "CAD",
                    "is_pending": 0,
                    "source": "scraper_rbc_csv",
                }
            )

        return results

    except Exception:
        return []
