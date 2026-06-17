"""Wealthica REST API client with fixture/mock mode.

Mock mode activates automatically when WEALTHICA_CLIENT_ID is not set.
Returns realistic Canadian banking data for development and testing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE_URL = "https://api.wealthica.com"

# ---------------------------------------------------------------------------
# Mock fixture data — returned when WEALTHICA_CLIENT_ID is not set
# ---------------------------------------------------------------------------

_TODAY = date.today().isoformat()
_30D_AGO = (date.today() - timedelta(days=30)).isoformat()
_60D_AGO = (date.today() - timedelta(days=60)).isoformat()
_90D_AGO = (date.today() - timedelta(days=90)).isoformat()


def _mock_institutions() -> list[dict]:
    return [
        {
            "_id": "mock-rbc-chequing",
            "institution": "RBC",
            "name": "RBC Chequing",
            "type": "bank",
            "currency": "CAD",
            "balance": 2840.00,
        },
        {
            "_id": "mock-rbc-savings",
            "institution": "RBC",
            "name": "RBC Savings",
            "type": "bank",
            "currency": "CAD",
            "balance": 500.00,
        },
        {
            "_id": "mock-td-visa",
            "institution": "TD",
            "name": "TD Visa",
            "type": "credit",
            "currency": "CAD",
            "balance": 1240.00,
        },
        {
            "_id": "mock-desjardins-mortgage",
            "institution": "Desjardins",
            "name": "Desjardins Mortgage",
            "type": "loan",
            "currency": "CAD",
            "balance": 285000.00,
        },
        {
            "_id": "mock-wealthsimple-cash",
            "institution": "Wealthsimple",
            "name": "Wealthsimple Cash",
            "type": "bank",
            "currency": "CAD",
            "balance": 0.00,
        },
    ]


def _mock_transactions(start_date: str, end_date: str, institution_id: str | None = None) -> list[dict]:
    all_txns = [
        # RBC Chequing income + expenses
        {
            "_id": "mock-txn-salary-001",
            "account": "mock-rbc-chequing",
            "date": _30D_AGO,
            "amount": 2800.00,
            "description": "EMPLOYER PAYROLL DIRECT DEPOSIT",
            "category": "Income",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-rent-001",
            "account": "mock-rbc-chequing",
            "date": _30D_AGO,
            "amount": -2140.00,
            "description": "DESJARDINS MORTGAGE PAYMENT",
            "category": "Housing",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-grocery-001",
            "account": "mock-rbc-chequing",
            "date": (date.today() - timedelta(days=5)).isoformat(),
            "amount": -145.50,
            "description": "METRO GROCERIES MONTREAL",
            "category": "Groceries",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-grocery-002",
            "account": "mock-rbc-chequing",
            "date": (date.today() - timedelta(days=12)).isoformat(),
            "amount": -187.30,
            "description": "METRO GROCERIES MONTREAL",
            "category": "Groceries",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-grocery-003",
            "account": "mock-rbc-chequing",
            "date": _30D_AGO,
            "amount": -156.80,
            "description": "METRO GROCERIES MONTREAL",
            "category": "Groceries",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-gas-001",
            "account": "mock-rbc-chequing",
            "date": (date.today() - timedelta(days=3)).isoformat(),
            "amount": -78.40,
            "description": "ESSO GAS STATION",
            "category": "Transportation",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-inheritance-001",
            "account": "mock-rbc-chequing",
            "date": _30D_AGO,
            "amount": 1300.00,
            "description": "ESTATE TRANSFER DEPOSIT",
            "category": "Income",
            "currency": "CAD",
            "pending": False,
        },
        # TD Visa credit card charges
        {
            "_id": "mock-txn-netflix-001",
            "account": "mock-td-visa",
            "date": (date.today() - timedelta(days=2)).isoformat(),
            "amount": -17.99,
            "description": "NETFLIX.COM",
            "category": "Entertainment",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-netflix-002",
            "account": "mock-td-visa",
            "date": _30D_AGO,
            "amount": -17.99,
            "description": "NETFLIX.COM",
            "category": "Entertainment",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-netflix-003",
            "account": "mock-td-visa",
            "date": _60D_AGO,
            "amount": -17.99,
            "description": "NETFLIX.COM",
            "category": "Entertainment",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-spotify-001",
            "account": "mock-td-visa",
            "date": (date.today() - timedelta(days=4)).isoformat(),
            "amount": -10.99,
            "description": "SPOTIFY PREMIUM",
            "category": "Entertainment",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-spotify-002",
            "account": "mock-td-visa",
            "date": _30D_AGO,
            "amount": -10.99,
            "description": "SPOTIFY PREMIUM",
            "category": "Entertainment",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-spotify-003",
            "account": "mock-td-visa",
            "date": _60D_AGO,
            "amount": -10.99,
            "description": "SPOTIFY PREMIUM",
            "category": "Entertainment",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-do-001",
            "account": "mock-td-visa",
            "date": (date.today() - timedelta(days=1)).isoformat(),
            "amount": -25.00,
            "description": "DIGITALOCEAN.COM",
            "category": "Technology",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-do-002",
            "account": "mock-td-visa",
            "date": _30D_AGO,
            "amount": -25.00,
            "description": "DIGITALOCEAN.COM",
            "category": "Technology",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-do-003",
            "account": "mock-td-visa",
            "date": _60D_AGO,
            "amount": -25.00,
            "description": "DIGITALOCEAN.COM",
            "category": "Technology",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-amazon-001",
            "account": "mock-td-visa",
            "date": (date.today() - timedelta(days=6)).isoformat(),
            "amount": -340.00,
            "description": "AMAZON.CA MARKETPLACE",
            "category": "Shopping",
            "currency": "CAD",
            "pending": False,
        },
        {
            "_id": "mock-txn-payment-001",
            "account": "mock-td-visa",
            "date": _30D_AGO,
            "amount": 800.00,
            "description": "TD VISA PAYMENT THANK YOU",
            "category": "Transfer",
            "currency": "CAD",
            "pending": False,
        },
    ]

    filtered = [
        t for t in all_txns
        if t["date"] >= start_date and t["date"] <= end_date
    ]
    if institution_id:
        filtered = [t for t in filtered if t["account"] == institution_id]
    return filtered


def _mock_history(institution_id: str | None = None) -> list[dict]:
    return [
        {
            "date": _30D_AGO,
            "institution": institution_id or "mock-rbc-chequing",
            "value": 2500.00,
        },
        {
            "date": _TODAY,
            "institution": institution_id or "mock-rbc-chequing",
            "value": 2840.00,
        },
    ]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _get(url: str, token: str, params: dict | None = None) -> list | dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _post(url: str, body: dict, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_token(client_id: str, secret: str, user_email: str) -> str:
    """POST /auth/token → bearer token string."""
    result = _post(
        f"{BASE_URL}/auth/token",
        body={"clientId": client_id, "secret": secret},
        headers={"loginName": user_email},
    )
    return result["token"]


def get_institutions(token: str | None) -> list[dict]:
    """GET /institutions → list of account dicts."""
    if token is None:
        return _mock_institutions()
    return _get(f"{BASE_URL}/institutions", token)  # type: ignore[return-value]


def get_transactions(
    token: str | None,
    start_date: str,
    end_date: str,
    institution_id: str | None = None,
) -> list[dict]:
    """GET /transactions → list of transaction dicts."""
    if token is None:
        return _mock_transactions(start_date, end_date, institution_id)
    params: dict = {"startDate": start_date, "endDate": end_date}
    if institution_id:
        params["institutionId"] = institution_id
    return _get(f"{BASE_URL}/transactions", token, params)  # type: ignore[return-value]


def get_history(token: str | None, institution_id: str | None = None) -> list[dict]:
    """GET /history → balance history list."""
    if token is None:
        return _mock_history(institution_id)
    params = {}
    if institution_id:
        params["institutionId"] = institution_id
    return _get(f"{BASE_URL}/history", token, params or None)  # type: ignore[return-value]
