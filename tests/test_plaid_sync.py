"""Unit tests for plaid_sync — no live API calls, no ~/.jarvis.env required."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[1]
for _p in (str(_REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from skills.finance.db import init_db  # noqa: E402
from skills.finance.plaid_sync import (  # noqa: E402
    _ensure_schema,
    _get_cursor,
    _get_tokens,
    _internal_id,
    _map_transaction,
    _parse_plaid_date,
    _save_cursor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    db = init_db(str(tmp_path / "test_finance.db"))
    _ensure_schema(db)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove any PLAID_* vars from env so tests are isolated."""
    for k in list(os.environ):
        if k.startswith("PLAID_"):
            monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# _parse_plaid_date
# ---------------------------------------------------------------------------


def test_parse_plaid_date_iso():
    assert _parse_plaid_date("2026-07-06") == "2026-07-06"


def test_parse_plaid_date_http_format():
    assert _parse_plaid_date("Mon, 06 Jul 2026 00:00:00 GMT") == "2026-07-06"


def test_parse_plaid_date_empty():
    assert _parse_plaid_date("") == ""


# ---------------------------------------------------------------------------
# _get_tokens
# ---------------------------------------------------------------------------


def test_get_tokens_none(monkeypatch):
    assert _get_tokens() == []


def test_get_tokens_one(monkeypatch):
    monkeypatch.setenv("PLAID_ACCESS_TOKEN_TD_VISA", "access-production-abc123")
    tokens = _get_tokens()
    assert len(tokens) == 1
    assert tokens[0] == ("TD_VISA", "access-production-abc123")


def test_get_tokens_skips_empty(monkeypatch):
    monkeypatch.setenv("PLAID_ACCESS_TOKEN_EMPTY", "")
    assert _get_tokens() == []


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------


def test_cursor_default_empty(conn):
    assert _get_cursor(conn, "TD_VISA") == ""


def test_cursor_roundtrip(conn):
    _save_cursor(conn, "TD_VISA", "cursor-abc")
    assert _get_cursor(conn, "TD_VISA") == "cursor-abc"


def test_cursor_update(conn):
    _save_cursor(conn, "TD_VISA", "cursor-v1")
    _save_cursor(conn, "TD_VISA", "cursor-v2")
    assert _get_cursor(conn, "TD_VISA") == "cursor-v2"


# ---------------------------------------------------------------------------
# Account mapping
# ---------------------------------------------------------------------------


def _seed_account_map(conn, plaid_id="plaid-abc", internal="plaid-td_visa-1225"):
    conn.execute(
        "INSERT OR IGNORE INTO plaid_account_map (plaid_account_id, internal_id, label, type) VALUES (?, ?, ?, ?)",
        (plaid_id, internal, "TD_VISA", "credit"),
    )
    conn.commit()


def test_internal_id_found(conn):
    _seed_account_map(conn)
    assert _internal_id(conn, "plaid-abc") == "plaid-td_visa-1225"


def test_internal_id_missing(conn):
    assert _internal_id(conn, "unknown-id") is None


# ---------------------------------------------------------------------------
# _map_transaction
# ---------------------------------------------------------------------------


def _raw_txn(**overrides):
    base = {
        "transaction_id": "txn-001",
        "account_id": "plaid-abc",
        "date": "2026-07-06",
        "amount": 42.50,
        "name": "SOME MERCHANT",
        "pending": False,
        "iso_currency_code": "CAD",
    }
    return {**base, **overrides}


def test_map_transaction_basic(conn):
    _seed_account_map(conn)
    txn = _map_transaction(conn, _raw_txn(), "TD_VISA")
    assert txn is not None
    assert txn["id"] == "plaid-txn-001"
    assert txn["amount"] == -42.50  # negated: Plaid positive = debit
    assert txn["date"] == "2026-07-06"
    assert txn["description"] == "SOME MERCHANT"
    assert txn["source"] == "plaid"
    assert txn["is_pending"] == 0


def test_map_transaction_negates_amount(conn):
    _seed_account_map(conn)
    txn = _map_transaction(conn, _raw_txn(amount=100.0), "TD_VISA")
    assert txn["amount"] == -100.0


def test_map_transaction_refund_stays_positive(conn):
    """Plaid returns refunds as negative amounts — negating gives positive (money in)."""
    _seed_account_map(conn)
    txn = _map_transaction(conn, _raw_txn(amount=-50.0), "TD_VISA")
    assert txn["amount"] == 50.0


def test_map_transaction_unknown_account_returns_none(conn):
    txn = _map_transaction(conn, _raw_txn(account_id="no-such-id"), "TD_VISA")
    assert txn is None


def test_map_transaction_pending_flag(conn):
    _seed_account_map(conn)
    txn = _map_transaction(conn, _raw_txn(pending=True), "TD_VISA")
    assert txn["is_pending"] == 1


def test_map_transaction_http_date(conn):
    _seed_account_map(conn)
    txn = _map_transaction(conn, _raw_txn(date="Mon, 06 Jul 2026 00:00:00 GMT"), "TD_VISA")
    assert txn["date"] == "2026-07-06"
