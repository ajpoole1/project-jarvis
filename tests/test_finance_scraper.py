"""Unit tests for finance scraper layer.

No real browser launched — Playwright is mocked throughout.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).parents[1]
for _p in (str(_REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from skills.finance.db import init_db, upsert_transaction  # noqa: E402
from skills.finance.scraper_errors import ScraperError, SessionExpiredError  # noqa: E402
from skills.finance.scraper_rbc import (  # noqa: E402
    _extract_transactions_from_payload,
    _normalize_amount,
    _txn_id,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    conn = init_db(str(tmp_path / "finance.db"))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# scraper_errors
# ---------------------------------------------------------------------------


def test_session_expired_error_is_exception():
    exc = SessionExpiredError("RBC session expired — run: finance scrape --bank rbc --first-auth")
    assert isinstance(exc, Exception)
    assert "first-auth" in str(exc)


def test_scraper_error_is_exception():
    exc = ScraperError("page structure changed")
    assert isinstance(exc, Exception)
    assert "page structure" in str(exc)


def test_session_expired_error_distinct_from_scraper_error():
    assert not issubclass(SessionExpiredError, ScraperError)
    assert not issubclass(ScraperError, SessionExpiredError)


# ---------------------------------------------------------------------------
# is_auth_expired (URL detection)
# ---------------------------------------------------------------------------


def test_is_auth_expired_detects_signin():
    from skills.finance.scraper_base import is_auth_expired

    page = MagicMock()
    page.url = "https://www.rbcroyalbank.com/cgi-bin/rbaccess/rbunxcgi?REQUEST=ClientSignin"
    assert asyncio.run(is_auth_expired(page)) is True


def test_is_auth_expired_detects_login():
    from skills.finance.scraper_base import is_auth_expired

    page = MagicMock()
    page.url = "https://example.com/login?redirect=/dashboard"
    assert asyncio.run(is_auth_expired(page)) is True


def test_is_auth_expired_passes_dashboard():
    from skills.finance.scraper_base import is_auth_expired

    page = MagicMock()
    page.url = "https://www1.royalbank.com/cgi-bin/rbaccess/rbunxcgi?REQUEST=AccountSummary"
    assert asyncio.run(is_auth_expired(page)) is False


# ---------------------------------------------------------------------------
# _normalize_amount
# ---------------------------------------------------------------------------


def test_normalize_amount_debit_is_negative():
    assert _normalize_amount(50.0, "debit") == -50.0


def test_normalize_amount_credit_is_positive():
    assert _normalize_amount(50.0, "credit") == 50.0


def test_normalize_amount_dr_variant():
    assert _normalize_amount(100.0, "DR") == -100.0


def test_normalize_amount_cr_variant():
    assert _normalize_amount(100.0, "CR") == 100.0


def test_normalize_amount_no_type_passthrough():
    assert _normalize_amount(-25.0, None) == -25.0
    assert _normalize_amount(25.0, None) == 25.0


# ---------------------------------------------------------------------------
# _txn_id
# ---------------------------------------------------------------------------


def test_txn_id_is_deterministic():
    a = _txn_id("2026-06-01", -45.99, "TIM HORTONS")
    b = _txn_id("2026-06-01", -45.99, "TIM HORTONS")
    assert a == b


def test_txn_id_differs_on_amount():
    a = _txn_id("2026-06-01", -45.99, "TIM HORTONS")
    b = _txn_id("2026-06-01", -46.00, "TIM HORTONS")
    assert a != b


def test_txn_id_is_sha256_hex():
    tid = _txn_id("2026-06-01", -10.0, "SHOPIFY")
    assert len(tid) == 64
    assert all(c in "0123456789abcdef" for c in tid)


def test_txn_id_prefixed_with_rbc():
    raw = "rbc:2026-06-01-10.00SHOPIFY"
    expected = hashlib.sha256(raw.encode()).hexdigest()
    assert _txn_id("2026-06-01", -10.0, "SHOPIFY") == expected


# ---------------------------------------------------------------------------
# _extract_transactions_from_payload
# ---------------------------------------------------------------------------


def test_extract_list_envelope():
    payload = [
        {
            "transactionDate": "2026-06-10",
            "amount": 25.0,
            "type": "debit",
            "description": "METRO INC",
            "accountNumber": "12345",
        }
    ]
    result = _extract_transactions_from_payload(payload, days=30)
    assert len(result) == 1
    assert result[0]["amount"] == -25.0
    assert result[0]["description"] == "METRO INC"
    assert result[0]["date"] == "2026-06-10"


def test_extract_dict_envelope():
    payload = {
        "transactions": [
            {
                "transactionDate": "2026-06-12",
                "amount": 100.0,
                "type": "credit",
                "description": "PAYROLL",
                "accountNumber": "99999",
            }
        ]
    }
    result = _extract_transactions_from_payload(payload, days=30)
    assert len(result) == 1
    assert result[0]["amount"] == 100.0


def test_extract_skips_old_transactions():
    payload = [
        {
            "transactionDate": "2020-01-01",
            "amount": 10.0,
            "type": "debit",
            "description": "OLD TXN",
            "accountNumber": "111",
        }
    ]
    result = _extract_transactions_from_payload(payload, days=30)
    assert result == []


def test_extract_empty_payload():
    assert _extract_transactions_from_payload([], days=30) == []
    assert _extract_transactions_from_payload({}, days=30) == []


# ---------------------------------------------------------------------------
# Dedup — scraper output routed through upsert_transaction
# ---------------------------------------------------------------------------


def test_scraper_dedup_skips_duplicate(db):
    txn = {
        "id": _txn_id("2026-06-10", -20.0, "STARBUCKS"),
        "account_id": "rbc-12345",
        "date": "2026-06-10",
        "amount": -20.0,
        "description": "STARBUCKS",
        "category": None,
        "currency": "CAD",
        "owner": "personal",
        "is_pending": 0,
        "source": "scraper_rbc",
        "note": None,
    }
    assert upsert_transaction(db, txn) is True
    assert upsert_transaction(db, txn) is False


def test_scraper_dedup_inserts_distinct(db):
    txn1 = {
        "id": _txn_id("2026-06-10", -20.0, "STARBUCKS"),
        "account_id": "rbc-12345",
        "date": "2026-06-10",
        "amount": -20.0,
        "description": "STARBUCKS",
        "category": None,
        "currency": "CAD",
        "owner": "personal",
        "is_pending": 0,
        "source": "scraper_rbc",
        "note": None,
    }
    txn2 = {
        "id": _txn_id("2026-06-11", -15.50, "TIM HORTONS"),
        "account_id": "rbc-12345",
        "date": "2026-06-11",
        "amount": -15.50,
        "description": "TIM HORTONS",
        "category": None,
        "currency": "CAD",
        "owner": "personal",
        "is_pending": 0,
        "source": "scraper_rbc",
        "note": None,
    }
    assert upsert_transaction(db, txn1) is True
    assert upsert_transaction(db, txn2) is True
