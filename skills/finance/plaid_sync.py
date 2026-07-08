"""Plaid sync — pull transactions via transactions_sync and upsert into finance.db.

Reads credentials from ~/.jarvis.env (loaded into os.environ by skill.py at startup,
or by _bootstrap_env() when this module is called standalone):

    PLAID_CLIENT_ID=<id>
    PLAID_SECRET=<production secret>
    PLAID_ACCESS_TOKEN_<LABEL>=access-production-...
    PLAID_ITEM_ID_<LABEL>=...           (optional, informational)

Sync cursors are persisted in plaid_cursors so each run fetches only
new/modified/removed transactions since the last call.

Amount sign convention (matches the rest of the finance skill):
    negative = money leaving  (purchases, fees)
    positive = money arriving (refunds, payments received)

Plaid reports all transactions as positive = debit (money out of the account).
We negate unconditionally to match our convention.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_PLAID_BASE = "https://production.plaid.com"


# ---------------------------------------------------------------------------
# Env bootstrap — reuses skill.py's _load_env logic when called standalone
# ---------------------------------------------------------------------------


def _bootstrap_env() -> None:
    """Load ~/.jarvis.env into os.environ if PLAID_CLIENT_ID is not yet set.

    When imported via skill.py this is a no-op — skill.py already calls _load_env
    at module level before any imports run.
    """
    if os.environ.get("PLAID_CLIENT_ID"):
        return
    env_path = Path.home() / ".jarvis.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            os.environ.setdefault(k, v)


_bootstrap_env()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _get_tokens() -> list[tuple[str, str]]:
    """Return [(label, access_token), ...] for all PLAID_ACCESS_TOKEN_* env vars."""
    tokens = []
    for k, v in os.environ.items():
        if k.startswith("PLAID_ACCESS_TOKEN_") and v:
            label = k[len("PLAID_ACCESS_TOKEN_") :]
            tokens.append((label, v))
    return tokens


def _get_client_creds() -> tuple[str, str]:
    return os.environ.get("PLAID_CLIENT_ID", ""), os.environ.get("PLAID_SECRET", "")


# ---------------------------------------------------------------------------
# Plaid HTTP (stdlib only — no plaid_python dependency)
# ---------------------------------------------------------------------------


def _plaid_post(endpoint: str, payload: dict) -> dict:
    client_id, secret = _get_client_creds()
    body = json.dumps({**payload, "client_id": client_id, "secret": secret}).encode()
    req = urllib.request.Request(
        f"{_PLAID_BASE}{endpoint}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Plaid {endpoint} HTTP {e.code}: {body_text}") from e


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS plaid_cursors (
            label       TEXT PRIMARY KEY,
            cursor      TEXT NOT NULL DEFAULT '',
            last_synced TEXT
        );

        CREATE TABLE IF NOT EXISTS plaid_account_map (
            plaid_account_id  TEXT PRIMARY KEY,
            internal_id       TEXT NOT NULL,
            label             TEXT NOT NULL,
            name              TEXT,
            type              TEXT,
            subtype           TEXT
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Account mapping
# ---------------------------------------------------------------------------


def _fetch_and_map_accounts(conn: sqlite3.Connection, label: str, access_token: str) -> None:
    """Pull /accounts/get and populate plaid_account_map for any new accounts.

    Rows are write-once (INSERT OR IGNORE) — if an account's mask or name changes
    upstream, manually update plaid_account_map or drop the row to force a refresh.
    """
    resp = _plaid_post("/accounts/get", {"access_token": access_token})
    for acct in resp.get("accounts", []):
        plaid_id = acct["account_id"]
        mask = acct.get("mask") or ""
        internal_id = (
            f"plaid-{label.lower()}-{mask}" if mask else f"plaid-{label.lower()}-{plaid_id[-4:]}"
        )
        conn.execute(
            """INSERT OR IGNORE INTO plaid_account_map
               (plaid_account_id, internal_id, label, name, type, subtype)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (plaid_id, internal_id, label, acct.get("name"), acct.get("type"), acct.get("subtype")),
        )
    conn.commit()


def _internal_id(conn: sqlite3.Connection, plaid_account_id: str) -> str | None:
    row = conn.execute(
        "SELECT internal_id FROM plaid_account_map WHERE plaid_account_id = ?",
        (plaid_account_id,),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------


def _get_cursor(conn: sqlite3.Connection, label: str) -> str:
    row = conn.execute("SELECT cursor FROM plaid_cursors WHERE label = ?", (label,)).fetchone()
    return row[0] if row else ""


def _save_cursor(conn: sqlite3.Connection, label: str, cursor: str) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO plaid_cursors (label, cursor, last_synced)
           VALUES (?, ?, ?)
           ON CONFLICT(label) DO UPDATE SET cursor = excluded.cursor, last_synced = excluded.last_synced""",
        (label, cursor, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Transaction mapping
# ---------------------------------------------------------------------------


def _parse_plaid_date(date_str: str) -> str:
    """Normalize Plaid date strings to YYYY-MM-DD.

    Plaid may return either ISO format ('2026-07-06') or HTTP date format
    ('Mon, 06 Jul 2026 00:00:00 GMT'). Returns the input unchanged if neither parses.
    """
    if not date_str:
        return date_str
    if "," in date_str:
        try:
            return datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %Z").strftime("%Y-%m-%d")
        except ValueError:
            return date_str[:10]
    return date_str[:10]


def _map_transaction(conn: sqlite3.Connection, raw: dict, label: str) -> dict | None:
    """Convert a raw Plaid transaction to our DB schema.

    Plaid amount sign convention: positive = money leaving the account (debit/purchase).
    We negate unconditionally so negative = spend, matching the rest of the finance skill.
    Returns None if the Plaid account_id has no mapping in plaid_account_map.
    """
    plaid_account_id = raw.get("account_id", "")
    internal_id = _internal_id(conn, plaid_account_id)
    if not internal_id:
        log.warning(
            "No account map entry for plaid_account_id=%s (label=%s)", plaid_account_id, label
        )
        return None

    amount = -(raw.get("amount", 0.0))  # Plaid positive = debit; we want negative = spend
    txn_id = raw.get("transaction_id", "")
    name = raw.get("name") or raw.get("merchant_name") or ""

    return {
        "id": f"plaid-{txn_id}",
        "account_id": internal_id,
        "date": _parse_plaid_date(raw.get("date", "")),
        "amount": amount,
        "description": name,
        "category": None,
        "currency": raw.get("iso_currency_code") or "CAD",
        "owner": "personal",
        "is_pending": int(raw.get("pending", False)),
        "source": "plaid",
        "note": None,
    }


# ---------------------------------------------------------------------------
# Core sync loop
# ---------------------------------------------------------------------------


def sync_item(conn: sqlite3.Connection, label: str, access_token: str) -> dict:
    """Run transactions_sync for one item. Returns summary dict."""
    _ensure_schema(conn)
    _fetch_and_map_accounts(conn, label, access_token)

    cursor = _get_cursor(conn, label)
    added = modified = removed = 0
    has_more = True

    while has_more:
        payload: dict = {"access_token": access_token}
        if cursor:
            payload["cursor"] = cursor

        resp = _plaid_post("/transactions/sync", payload)
        has_more = resp.get("has_more", False)
        cursor = resp.get("next_cursor", cursor)

        for raw in resp.get("added", []):
            txn = _map_transaction(conn, raw, label)
            if txn:
                from skills.finance.db import upsert_transaction

                if upsert_transaction(conn, txn):
                    added += 1

        for raw in resp.get("modified", []):
            txn = _map_transaction(conn, raw, label)
            if txn:
                conn.execute("DELETE FROM transactions WHERE id = ?", (txn["id"],))
                conn.commit()
                from skills.finance.db import upsert_transaction

                upsert_transaction(conn, txn)
                modified += 1

        for raw in resp.get("removed", []):
            txn_id = f"plaid-{raw.get('transaction_id', '')}"
            conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
            conn.commit()
            removed += 1

    _save_cursor(conn, label, cursor)
    log.info("sync_item %s: +%d added, ~%d modified, -%d removed", label, added, modified, removed)
    return {"label": label, "added": added, "modified": modified, "removed": removed}


def cmd_plaid_sync(conn: sqlite3.Connection) -> str:
    """Sync all configured Plaid items into finance.db."""
    tokens = _get_tokens()
    if not tokens:
        return (
            "No Plaid access tokens found in ~/.jarvis.env. Expected PLAID_ACCESS_TOKEN_<LABEL>=..."
        )

    lines = []
    total_added = 0
    for label, token in tokens:
        try:
            result = sync_item(conn, label, token)
            lines.append(
                f"  {label}: +{result['added']} added, "
                f"~{result['modified']} modified, "
                f"-{result['removed']} removed"
            )
            total_added += result["added"]
        except Exception as e:
            log.error("plaid_sync error for %s: %s", label, e)
            lines.append(f"  {label}: ERROR — {e}")

    if total_added > 0:
        from skills.finance.db import apply_finance_rules_all

        updated = apply_finance_rules_all(conn)
        lines.append(f"  rules applied: {updated} transaction(s) classified")

    return "Plaid sync complete:\n" + "\n".join(lines)
