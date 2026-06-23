"""Finance database layer — SQLite with finance.db schema.

Amount sign convention (enforced throughout):
  negative = money out (debit / spend / payment leaving bank)
  positive = money in  (income / deposit / CC payment received)

SUM(amount) < 0 means you spent more than came in.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    id                   TEXT PRIMARY KEY,
    institution          TEXT NOT NULL,
    name                 TEXT,
    type                 TEXT,
    currency             TEXT DEFAULT 'CAD',
    balance_current      REAL,
    balance_anchor       REAL,
    balance_anchor_date  TEXT,
    owner                TEXT DEFAULT 'personal',
    source               TEXT DEFAULT 'wealthica',
    last_synced          TEXT
);

-- Migration: add anchor columns if this is an existing DB
CREATE INDEX IF NOT EXISTS idx_accounts_id ON accounts(id);

CREATE TABLE IF NOT EXISTS transactions (
    id          TEXT PRIMARY KEY,
    account_id  TEXT REFERENCES accounts(id),
    date        TEXT NOT NULL,
    amount      REAL NOT NULL,
    description TEXT,
    category    TEXT,
    currency    TEXT DEFAULT 'CAD',
    owner       TEXT DEFAULT 'personal',
    is_pending  INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'wealthica',
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_txn_date       ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_account    ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_txn_owner      ON transactions(owner);
CREATE INDEX IF NOT EXISTS idx_txn_category   ON transactions(category);

CREATE TABLE IF NOT EXISTS recurring (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_norm   TEXT NOT NULL,
    amount_median   REAL,
    cadence_days    INTEGER,
    last_seen       TEXT,
    next_expected   TEXT,
    owner           TEXT DEFAULT 'personal',
    category        TEXT
);

-- Legacy simple-pattern rules table (retained for migration only; superseded by finance_rules).
CREATE TABLE IF NOT EXISTS rules (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern  TEXT NOT NULL,
    owner    TEXT NOT NULL,
    category TEXT,
    created  TEXT
);

-- Rich data-driven rules.  Priority descending = most specific wins.
-- match_amount_min/max: range only — no exact-equality match (USD-billed charges drift).
CREATE TABLE IF NOT EXISTS finance_rules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    match_merchant    TEXT NOT NULL,
    match_descriptor  TEXT,
    match_recurrence  TEXT,
    match_amount_min  REAL,
    match_amount_max  REAL,
    category          TEXT,
    owner             TEXT DEFAULT 'personal',
    is_transfer       INTEGER DEFAULT 0,
    priority          INTEGER DEFAULT 0,
    note              TEXT,
    updated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_frules_merchant ON finance_rules(match_merchant);

CREATE TABLE IF NOT EXISTS sync_state (
    key     TEXT PRIMARY KEY,
    value   TEXT,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,  -- account_id:symbol
    account_id      TEXT REFERENCES accounts(id),
    symbol          TEXT NOT NULL,
    name            TEXT,
    quantity        REAL,
    market_price    REAL,
    price_currency  TEXT DEFAULT 'CAD',
    market_value_native REAL,
    market_value_cad    REAL,
    book_value_cad      REAL,
    unrealized_cad      REAL,
    as_of           TEXT
);

CREATE INDEX IF NOT EXISTS idx_pos_account ON positions(account_id);

CREATE TABLE IF NOT EXISTS goals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    target_amount  REAL,
    current_amount REAL DEFAULT 0,
    account_id     TEXT REFERENCES accounts(id),
    notes          TEXT
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables if not exist. Returns open connection with row_factory."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    _migrate(conn)
    conn.commit()
    _seed_transfer_rules(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema migrations for existing DBs."""
    acct_cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "balance_anchor" not in acct_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN balance_anchor REAL")
    if "balance_anchor_date" not in acct_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN balance_anchor_date TEXT")
    # positions table created by DDL above on new DBs; nothing to migrate for existing ones

    # Migrate legacy rules → finance_rules if finance_rules is empty and rules has rows.
    fr_count = conn.execute("SELECT COUNT(*) FROM finance_rules").fetchone()[0]
    legacy_count = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    if fr_count == 0 and legacy_count > 0:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        rows = conn.execute("SELECT pattern, owner, category FROM rules").fetchall()
        for row in rows:
            is_transfer = (
                1
                if (row["category"] or "").endswith("_transfer")
                or row["category"]
                in ("loan_payment", "inheritance_deposit", "insurance_reimbursement")
                else 0
            )
            conn.execute(
                "INSERT INTO finance_rules "
                "(match_merchant, category, owner, is_transfer, priority, updated_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (row["pattern"], row["category"], row["owner"], is_transfer, now),
            )
        conn.commit()


# Transfer/internal patterns that should never count as spend.
# These are seeded once at DB init and never duplicated (INSERT OR IGNORE).
_TRANSFER_RULES: list[tuple[str, str]] = [
    # RBC internal web transfers
    ("WWW TRF DDA%", "internal_transfer"),
    ("WWW TFR VIN0%", "internal_transfer"),
    ("WWW TRANSFER TO DEPOSIT ACCOUNT%", "internal_transfer"),
    ("ONLINE TRANSFER TO DEPOSIT ACCOUNT%", "internal_transfer"),
    ("AUTO TRANSFER TO DEPOSIT ACCOUNT%", "internal_transfer"),
    ("AUTO TRANSFER FROM FIND & SAVE%", "internal_transfer"),
    # Nomi round-up auto-transfers
    ("FIND & SAVE%", "internal_transfer"),
    # RBC loan/LOC payments (obligation, not discretionary spend)
    ("Loan Pmt WWW LOAN PMT%", "loan_payment"),
    ("LOAN TD LOAN PAYMNT%", "loan_payment"),
    # Polina mortgage clearing e-transfer
    ("E-TRANSFER SENT POLINA%", "internal_transfer"),
    # WWW PMT = payment received from own account to LOC
    ("WWW PMT VIN0%", "internal_transfer"),
    # RBC online transfers (both description forms — dedup canonical handles import-time)
    ("ONLINE BANKING TRANSFER%", "internal_transfer"),
    ("Transfer WWW TRANSFER%", "internal_transfer"),
    # TD Visa CC payments-in: payments from RBC showing as income on the card account
    ("ROYAL BANK OF CANADA", "internal_transfer"),
    ("PAYMENT - THANK YOU%", "internal_transfer"),
    ("PAYMENT%THANK YOU%", "internal_transfer"),
    # RBC paying TD Visa / MBNA / Flexiti from chequing
    ("ONLINE BANKING PAYMENT%VISA TD BANK%", "internal_transfer"),
    ("Payment WWW PAYMENT%VISA TD BANK%", "internal_transfer"),
    ("ONLINE BANKING PAYMENT%MBNA-MASTERCARD%", "internal_transfer"),
    ("Payment WWW PAYMENT%MBNA-MASTERCARD%", "internal_transfer"),
    ("ONLINE BANKING PAYMENT%FLEXITI%", "internal_transfer"),
    ("Payment WWW PAYMENT%FLEXITI%", "internal_transfer"),
    # TD chequing internal transfers (TFR-TO C/C = TD paying TD Visa directly)
    ("%TFR-TO C/C%", "internal_transfer"),
    # TD chequing outbound e-transfers to RBC (Ricky's money moves + AJ self-transfers)
    ("SEND E-TFR%", "internal_transfer"),
    # RBC receiving those e-transfers back (E-TRANSFER RECEIVED AARON-JACOB = self)
    ("E-TRANSFER RECEIVED AARON-JACOB%", "internal_transfer"),
    # Nomi TO FIND & SAVE (debit side of round-up, pairs with FIND & SAVE% above)
    ("TO FIND & SAVE%", "internal_transfer"),
    ("FIND&SAVE FROM PDA%", "internal_transfer"),
    # Nomi round-up transfer (no-space variant)
    ("FIND&SAVE TRANSFER%", "internal_transfer"),
    ("%FIND&SAVE TRANSFER%", "internal_transfer"),
    # Joint chequing loan payments to Polina's LoC
    ("ONLINE BANKING LOAN PAYMENT%", "loan_payment"),
    # MBNA payment received (CC payment-in from own chequing) — exact match only
    ("PAYMENT", "internal_transfer"),
    # MBNA: "PAYMENT " with trailing space also seen
    ("PAYMENT ", "internal_transfer"),
    # TD chequing: PTS FRM = Ricky's money inheritance deposit
    ("PTS FRM%", "inheritance_deposit"),
    # TD chequing: GC transfer = AJ paying TD Visa from TD chequing directly
    ("GC%TRANSFER%", "internal_transfer"),
]

# Category rules for real spend — seeded alongside transfer rules.
_CATEGORY_RULES: list[tuple[str, str]] = [
    # Income
    ("PAYROLL DEPOSIT%", "income"),
    ("HEALTH/DENTAL CLAIM MANULIFE%", "insurance_reimbursement"),
    ("DEPOSIT INTEREST%", "interest_income"),
    ("BONUS DEP INT%", "interest_income"),
    ("BONUS DEPOSIT INTEREST%", "interest_income"),
    ("REV EXTRA DEBIT%", "interest_income"),
    ("INTERAC ABM FEE CR%", "bank_fee_refund"),
    # Groceries
    ("IGA%", "groceries"),
    ("COSTCO%", "groceries"),
    ("METRO%", "groceries"),
    ("MAXI%", "groceries"),
    ("PROVIGO%", "groceries"),
    ("SUPER C%", "groceries"),
    ("MARCHE%", "groceries"),
    # Gas / fuel
    ("FILGO%", "gas"),
    ("GAZ PROPANE%", "gas"),
    ("HARDIL GAS%", "gas"),
    ("PETRO%", "gas"),
    ("ESSO%", "gas"),
    ("SHELL%", "gas"),
    ("ULTRAMAR%", "gas"),
    # Dining / café
    ("SQ *TOMPOL%", "dining"),
    ("RESTAURANT%", "dining"),
    ("PIZZA%", "dining"),
    ("UBER CANADA/UBEREATS%", "dining"),
    ("UBER%EATS%", "dining"),
    ("DOORDASH%", "dining"),
    ("MCDONALDS%", "dining"),
    ("TIM HORTONS%", "dining"),
    ("STARBUCKS%", "dining"),
    # Utilities
    ("HYDRO BILL PMT HYDRO-QUEBEC%", "utilities"),
    ("HYDRO%QUEBEC%", "utilities"),
    ("BILL PAYMENT BELL CANADA%", "utilities"),
    ("BELL MEDIA%", "utilities"),
    ("BILL PAYMENT TELUS%", "utilities"),
    ("INTERNET%", "utilities"),
    ("ACCOUNT PAYABLE PMT RDE%", "utilities"),  # unknown recurring ~$212/mo — flag for review
    # Insurance
    ("INSURANCE DESJARDINS%", "insurance"),
    ("INSURANCE DAG%", "insurance"),
    ("LOANPROTECTOR INSURANCE%", "insurance"),
    # Healthcare / therapy
    ("E-TRANSFER SENT LAUREN THERAPY%", "healthcare"),
    ("E-TRANSFER SENT FAMILY THERAPY%", "healthcare"),
    ("BELLA DENTAIRE%", "healthcare"),
    ("PHARMAPRIX%", "healthcare"),
    ("JEAN COUTU%", "healthcare"),
    ("SHOPPERS DRUG MART%", "healthcare"),
    # Home / hardware
    ("THE HOME DEPOT%", "home_improvement"),
    ("RONA%", "home_improvement"),
    ("CANADIAN TIRE%", "home_improvement"),
    ("PEPINIERE%", "home_improvement"),
    ("VALLEE%FILS%", "home_improvement"),  # plumber / french drain
    # Hobbies / collectibles
    ("SP+AFF* HOBBIESVILLE%", "hobbies"),
    ("UNIVERSE COLLECTIBLES%", "hobbies"),
    ("DRAGON MEDIA ENT%", "hobbies"),
    # Education
    ("ATHABASCA U%", "education"),
    # BNPL / installment (home furniture)
    ("MISC PAYMENT AFFIRM%", "bnpl_payment"),
    # Subscriptions / software
    ("ANTHROPIC%CLAUDE%", "subscriptions"),
    ("CLAUDE AI%", "subscriptions"),
    ("AMAZON CHANNELS%", "subscriptions"),
    ("MICROSOFT%STORE%", "subscriptions"),
    ("MICROSOFT%", "subscriptions"),
    ("APPLE.COM%", "subscriptions"),
    ("NETFLIX%", "subscriptions"),
    ("SPOTIFY%", "subscriptions"),
    ("GOOGLE%", "subscriptions"),
    # Property tax
    ("%ST-LAZARE-TAX%", "property_tax"),
    ("%ST LAZARE TAX%", "property_tax"),
    # Taxes / government
    ("ONLINE BANKING PAYMENT%CRA%", "taxes"),
    ("Payment WWW PAYMENT%CRA%", "taxes"),
    ("ONLINE BANKING PAYMENT%REV QC%", "taxes"),
    ("Payment WWW PAYMENT%REV QC%", "taxes"),
    # Student loan (now paid off)
    ("ONLINE BANKING PAYMENT%NAT STU LN%", "loan_payment"),
    ("Payment WWW PAYMENT%NAT STU LN%", "loan_payment"),
]


def _seed_transfer_rules(conn: sqlite3.Connection) -> None:
    """Seed transfer and category rules into both rules (legacy) and finance_rules."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    _transfer_cats = {c for _, c in _TRANSFER_RULES}

    for pattern, category in _TRANSFER_RULES + _CATEGORY_RULES:
        existing = conn.execute("SELECT 1 FROM rules WHERE pattern = ?", (pattern,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO rules (pattern, owner, category, created) VALUES (?, 'personal', ?, ?)",
                (pattern, category, now),
            )
        fr_existing = conn.execute(
            "SELECT 1 FROM finance_rules WHERE match_merchant = ? AND match_descriptor IS NULL "
            "AND match_recurrence IS NULL",
            (pattern,),
        ).fetchone()
        if not fr_existing:
            is_transfer = 1 if category in _transfer_cats else 0
            conn.execute(
                "INSERT INTO finance_rules "
                "(match_merchant, category, owner, is_transfer, priority, updated_at) "
                "VALUES (?, ?, 'personal', ?, 0, ?)",
                (pattern, category, is_transfer, now),
            )
    conn.commit()

    # Seed owner-split rules for altaforma tech spend (priority=10 > default=0)
    _ALTAFORMA_SEEDS = [
        # Anthropic API usage (variable billing — altaforma)
        (
            "ANTHROPIC%",
            None,
            "variable",
            "subscriptions",
            "altaforma",
            0,
            10,
            "Anthropic API usage billing — altaforma",
        ),
        # Anthropic Max subscription (fixed monthly — personal). priority=20 beats the above.
        (
            "ANTHROPIC%CLAUDE%",
            None,
            "fixed_monthly",
            "subscriptions",
            "personal",
            0,
            20,
            "Anthropic Claude Max subscription — personal",
        ),
        # Cloud / dev infra — altaforma
        (
            "GOOGLE%CLOUD%",
            None,
            None,
            "cloud_infra",
            "altaforma",
            0,
            10,
            "Google Cloud Platform — altaforma",
        ),
        ("CLOUDFLARE%", None, None, "cloud_infra", "altaforma", 0, 10, "Cloudflare — altaforma"),
        ("AMAZON WEB SERVICES%", None, None, "cloud_infra", "altaforma", 0, 10, "AWS — altaforma"),
        ("AWS%", None, None, "cloud_infra", "altaforma", 0, 10, "AWS — altaforma"),
        ("GITHUB%", None, None, "subscriptions", "altaforma", 0, 10, "GitHub — altaforma"),
    ]
    for (
        merchant,
        descriptor,
        recurrence,
        category,
        owner,
        is_transfer,
        priority,
        note,
    ) in _ALTAFORMA_SEEDS:
        exists = conn.execute(
            "SELECT 1 FROM finance_rules WHERE match_merchant = ? AND match_recurrence IS ? "
            "AND owner = ?",
            (merchant, recurrence, owner),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO finance_rules "
                "(match_merchant, match_descriptor, match_recurrence, category, owner, "
                "is_transfer, priority, note, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    merchant,
                    descriptor,
                    recurrence,
                    category,
                    owner,
                    is_transfer,
                    priority,
                    note,
                    now,
                ),
            )
    conn.commit()


# ---------------------------------------------------------------------------
# finance_rules management
# ---------------------------------------------------------------------------


def upsert_finance_rule(conn: sqlite3.Connection, rule: dict) -> int:
    """Insert or update a finance_rules row. Returns the row id."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    existing_id = rule.get("id")
    if existing_id:
        conn.execute(
            "UPDATE finance_rules SET match_merchant=?, match_descriptor=?, match_recurrence=?, "
            "match_amount_min=?, match_amount_max=?, category=?, owner=?, is_transfer=?, "
            "priority=?, note=?, updated_at=? WHERE id=?",
            (
                rule["match_merchant"],
                rule.get("match_descriptor"),
                rule.get("match_recurrence"),
                rule.get("match_amount_min"),
                rule.get("match_amount_max"),
                rule.get("category"),
                rule.get("owner", "personal"),
                int(rule.get("is_transfer", 0)),
                rule.get("priority", 0),
                rule.get("note"),
                now,
                existing_id,
            ),
        )
        conn.commit()
        return existing_id
    cur = conn.execute(
        "INSERT INTO finance_rules "
        "(match_merchant, match_descriptor, match_recurrence, match_amount_min, match_amount_max, "
        "category, owner, is_transfer, priority, note, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rule["match_merchant"],
            rule.get("match_descriptor"),
            rule.get("match_recurrence"),
            rule.get("match_amount_min"),
            rule.get("match_amount_max"),
            rule.get("category"),
            rule.get("owner", "personal"),
            int(rule.get("is_transfer", 0)),
            rule.get("priority", 0),
            rule.get("note"),
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_finance_rules(conn: sqlite3.Connection, merchant_filter: str | None = None) -> list[dict]:
    """Return finance_rules rows, optionally filtered by merchant substring."""
    if merchant_filter:
        rows = conn.execute(
            "SELECT * FROM finance_rules WHERE match_merchant LIKE ? ORDER BY priority DESC, id",
            (f"%{merchant_filter}%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM finance_rules ORDER BY priority DESC, id").fetchall()
    return [dict(r) for r in rows]


def delete_finance_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    """Delete a finance_rules row. Returns True if a row was deleted."""
    cur = conn.execute("DELETE FROM finance_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def _match_finance_rule(rule: dict, txn: dict) -> bool:
    """Return True if a finance_rules row matches a transaction dict.

    Matching precedence (all non-null fields must match):
      1. match_merchant  — SQL LIKE pattern against description (required)
      2. match_descriptor — substring match against description (optional)
      3. match_recurrence — matched via recurring table or cadence signal (optional, best-effort)
      4. match_amount_min / match_amount_max — range on abs(amount) (optional)

    Amount range is the weakest signal and is checked last. No exact-equality
    amount match is ever performed — USD-billed charges drift each cycle.
    """
    desc = (txn.get("description") or "").upper()
    pattern = (rule.get("match_merchant") or "").upper()

    # SQL LIKE → Python: % = any, _ = one char
    import fnmatch

    like_pattern = pattern.replace("%", "*").replace("_", "?")
    if not fnmatch.fnmatch(desc, like_pattern):
        return False

    if rule.get("match_descriptor"):
        if rule["match_descriptor"].upper() not in desc:
            return False

    amt = abs(txn.get("amount", 0.0))
    if rule.get("match_amount_min") is not None and amt < rule["match_amount_min"]:
        return False
    if rule.get("match_amount_max") is not None and amt > rule["match_amount_max"]:
        return False

    return True


def apply_finance_rules(conn: sqlite3.Connection, txn_ids: list[str]) -> int:
    """Apply finance_rules to a list of transaction ids. Returns rows updated."""
    if not txn_ids:
        return 0
    rules = get_finance_rules(conn)
    placeholders = ",".join("?" * len(txn_ids))
    txns = conn.execute(
        f"SELECT id, description, amount FROM transactions WHERE id IN ({placeholders})",
        txn_ids,
    ).fetchall()

    updated = 0
    for txn in txns:
        txn_dict = dict(txn)
        for rule in rules:
            if _match_finance_rule(rule, txn_dict):
                set_parts = []
                params: list = []
                if rule.get("category"):
                    set_parts.append("category = ?")
                    params.append(rule["category"])
                if rule.get("owner"):
                    set_parts.append("owner = ?")
                    params.append(rule["owner"])
                if set_parts:
                    params.append(txn_dict["id"])
                    conn.execute(
                        f"UPDATE transactions SET {', '.join(set_parts)} WHERE id = ?", params
                    )
                    updated += 1
                break  # highest-priority rule wins; stop after first match
    conn.commit()
    return updated


def apply_finance_rules_all(conn: sqlite3.Connection) -> int:
    """Apply finance_rules to all transactions. Returns rows updated."""
    rules = get_finance_rules(conn)
    txns = conn.execute("SELECT id, description, amount FROM transactions").fetchall()

    updated = 0
    for txn in txns:
        txn_dict = dict(txn)
        for rule in rules:
            if _match_finance_rule(rule, txn_dict):
                set_parts = []
                params: list = []
                if rule.get("category"):
                    set_parts.append("category = ?")
                    params.append(rule["category"])
                if rule.get("owner"):
                    set_parts.append("owner = ?")
                    params.append(rule["owner"])
                if set_parts:
                    params.append(txn_dict["id"])
                    conn.execute(
                        f"UPDATE transactions SET {', '.join(set_parts)} WHERE id = ?", params
                    )
                    updated += 1
                break
    conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------


def upsert_account(conn: sqlite3.Connection, account: dict) -> None:
    """Insert or update an account record.

    Anchor fields (balance_anchor, balance_anchor_date) are only written when
    explicitly present in the dict — never clobbered by a plain import upsert.
    balance_current uses COALESCE so a null from CSV import never overwrites a
    manually stamped value.
    """
    conn.execute(
        """
        INSERT INTO accounts (
            id, institution, name, type, currency,
            balance_current, balance_anchor, balance_anchor_date,
            owner, source, last_synced
        )
        VALUES (
            :id, :institution, :name, :type, :currency,
            :balance_current, :balance_anchor, :balance_anchor_date,
            :owner, :source, :last_synced
        )
        ON CONFLICT(id) DO UPDATE SET
            institution         = excluded.institution,
            name                = excluded.name,
            type                = excluded.type,
            currency            = excluded.currency,
            balance_current     = COALESCE(excluded.balance_current, accounts.balance_current),
            balance_anchor      = COALESCE(excluded.balance_anchor, accounts.balance_anchor),
            balance_anchor_date = COALESCE(excluded.balance_anchor_date, accounts.balance_anchor_date),
            last_synced         = excluded.last_synced
        """,
        {
            "id": account.get("id"),
            "institution": account.get("institution", "Unknown"),
            "name": account.get("name"),
            "type": account.get("type"),
            "currency": account.get("currency", "CAD"),
            "balance_current": account.get("balance_current"),
            "balance_anchor": account.get("balance_anchor"),
            "balance_anchor_date": account.get("balance_anchor_date"),
            "owner": account.get("owner", "personal"),
            "source": account.get("source", "wealthica"),
            "last_synced": account.get("last_synced"),
        },
    )
    conn.commit()


def set_balance_anchor(
    conn: sqlite3.Connection, account_id: str, balance: float, as_of: str
) -> None:
    """Stamp a known-good balance anchor for an account.

    This is the reference point from which recompute_balances derives the
    current balance using SUM(transactions since anchor_date).
    Also sets balance_current to the anchor value immediately.
    """
    conn.execute(
        """UPDATE accounts
           SET balance_anchor = ?, balance_anchor_date = ?, balance_current = ?, last_synced = ?
           WHERE id = ?""",
        (balance, as_of, balance, as_of, account_id),
    )
    conn.commit()


def recompute_balances(conn: sqlite3.Connection) -> dict[str, float]:
    """Recompute balance_current for every account that has an anchor.

    Formula: balance_current = anchor + SUM(txn.amount WHERE date > anchor_date)
    Accounts without an anchor are left unchanged.
    Returns {account_id: new_balance} for accounts that were updated.
    """
    anchored = conn.execute(
        "SELECT id, balance_anchor, balance_anchor_date FROM accounts "
        "WHERE balance_anchor IS NOT NULL AND balance_anchor_date IS NOT NULL"
    ).fetchall()

    updated: dict[str, float] = {}
    today = date.today().isoformat()

    for row in anchored:
        aid = row["id"]
        anchor = row["balance_anchor"]
        anchor_date = row["balance_anchor_date"]

        delta_row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions "
            "WHERE account_id = ? AND date > ?",
            (aid, anchor_date),
        ).fetchone()
        delta = delta_row[0]
        new_balance = anchor + delta

        conn.execute(
            "UPDATE accounts SET balance_current = ?, last_synced = ? WHERE id = ?",
            (new_balance, today, aid),
        )
        updated[aid] = new_balance

    if updated:
        conn.commit()
    return updated


def get_accounts(conn: sqlite3.Connection) -> list[dict]:
    """Return all accounts as plain dicts."""
    rows = conn.execute("SELECT * FROM accounts ORDER BY type, institution, name").fetchall()
    return [dict(r) for r in rows]


def upsert_position(conn: sqlite3.Connection, pos: dict) -> None:
    conn.execute(
        """INSERT INTO positions (
               id, account_id, symbol, name, quantity,
               market_price, price_currency,
               market_value_native, market_value_cad,
               book_value_cad, unrealized_cad, as_of
           ) VALUES (
               :id, :account_id, :symbol, :name, :quantity,
               :market_price, :price_currency,
               :market_value_native, :market_value_cad,
               :book_value_cad, :unrealized_cad, :as_of
           )
           ON CONFLICT(id) DO UPDATE SET
               name                = excluded.name,
               quantity            = excluded.quantity,
               market_price        = excluded.market_price,
               price_currency      = excluded.price_currency,
               market_value_native = excluded.market_value_native,
               market_value_cad    = excluded.market_value_cad,
               book_value_cad      = excluded.book_value_cad,
               unrealized_cad      = excluded.unrealized_cad,
               as_of               = excluded.as_of
        """,
        pos,
    )


def get_positions(conn: sqlite3.Connection, account_id: str | None = None) -> list[dict]:
    if account_id:
        rows = conn.execute(
            "SELECT * FROM positions WHERE account_id = ? ORDER BY market_value_cad DESC",
            (account_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY account_id, market_value_cad DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------


def upsert_transaction(conn: sqlite3.Connection, txn: dict) -> bool:
    """Insert transaction if not already present.

    Returns True if new row was inserted, False if it already existed.
    For pending→cleared transitions, updates is_pending on the existing row.
    """
    existing = conn.execute(
        "SELECT id, is_pending FROM transactions WHERE id = ?", (txn["id"],)
    ).fetchone()
    if existing:
        if existing["is_pending"] and not txn.get("is_pending", 0):
            conn.execute("UPDATE transactions SET is_pending = 0 WHERE id = ?", (txn["id"],))
            conn.commit()
        return False

    conn.execute(
        """
        INSERT INTO transactions
            (id, account_id, date, amount, description, category, currency, owner, is_pending, source, note)
        VALUES
            (:id, :account_id, :date, :amount, :description, :category, :currency,
             :owner, :is_pending, :source, :note)
        """,
        {
            "id": txn["id"],
            "account_id": txn.get("account_id"),
            "date": txn.get("date"),
            "amount": txn.get("amount", 0.0),
            "description": txn.get("description", ""),
            "category": txn.get("category"),
            "currency": txn.get("currency", "CAD"),
            "owner": txn.get("owner", "personal"),
            "is_pending": int(txn.get("is_pending", 0)),
            "source": txn.get("source", "wealthica"),
            "note": txn.get("note"),
        },
    )
    conn.commit()
    return True


def get_transactions(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    owner: str | None = None,
    account_id: str | None = None,
) -> list[dict]:
    """Return transactions filtered by optional params, newest first."""
    clauses: list[str] = []
    params: list = []

    if start_date:
        clauses.append("date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("date <= ?")
        params.append(end_date)
    if owner:
        clauses.append("owner = ?")
        params.append(owner)
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM transactions {where} ORDER BY date DESC, id", params
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Recurring detection
# ---------------------------------------------------------------------------


_CADENCE_RANGES = [
    (5, 9, 7),  # weekly
    (12, 16, 14),  # biweekly
    (25, 35, 30),  # monthly
    (85, 100, 90),  # quarterly
]


def _normalize_merchant(desc: str | None) -> str:
    """Normalize a raw transaction description for merchant grouping."""
    if not desc:
        return "UNKNOWN"
    s = desc.upper()
    s = re.sub(r"[.\-/]", " ", s)
    s = re.sub(r"\b\d{4,}\b", "", s)
    # Strip trailing RBC e-transfer reference codes on E-TRANSFER SENT/RECEIVED rows.
    # These codes are 6-8 uppercase alphanumeric chars (e.g. "XFA7JD", "NNZJFG", "CANSFX8K").
    # Check after hyphen→space substitution above (E-TRANSFER → E TRANSFER).
    # Only strip on e-transfer rows — avoids clobbering real words elsewhere.
    if "E TRANSFER SENT" in s or "E TRANSFER RECEIVED" in s:
        s = re.sub(r"\s+[A-Z0-9]{6,8}$", "", s.rstrip())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:40].strip()


def _classify_cadence(median_days: float) -> int | None:
    for lo, hi, cadence in _CADENCE_RANGES:
        if lo <= median_days <= hi:
            return cadence
    return None


def detect_recurring(conn: sqlite3.Connection) -> int:
    """Analyse 180-day transaction history to find recurring charges.

    Clears and rebuilds the recurring table. Returns count of detected patterns.
    """
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    rows = conn.execute(
        "SELECT date, amount, description, owner, category FROM transactions "
        "WHERE date >= ? AND amount < 0 ORDER BY description, date",
        (cutoff,),
    ).fetchall()

    groups: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        norm = _normalize_merchant(row["description"])
        groups[norm].append((row["date"], row["amount"], row["owner"], row["category"]))

    conn.execute("DELETE FROM recurring")

    count = 0
    for merchant_norm, entries in groups.items():
        if len(entries) < 2:
            continue

        dates_sorted = sorted(e[0] for e in entries)
        intervals = [
            (date.fromisoformat(dates_sorted[i + 1]) - date.fromisoformat(dates_sorted[i])).days
            for i in range(len(dates_sorted) - 1)
        ]

        if not intervals:
            continue

        median_interval = statistics.median(intervals)
        cadence = _classify_cadence(median_interval)
        if cadence is None:
            continue

        amount_median = statistics.median(abs(e[1]) for e in entries)
        last_seen = max(dates_sorted)
        next_expected = (date.fromisoformat(last_seen) + timedelta(days=cadence)).isoformat()
        owner = entries[-1][2] or "personal"
        category = entries[-1][3]

        conn.execute(
            "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (merchant_norm, amount_median, cadence, last_seen, next_expected, owner, category),
        )
        count += 1

    conn.commit()
    return count


def get_recurring(conn: sqlite3.Connection) -> list[dict]:
    """Return all recurring charges sorted by next expected date."""
    rows = conn.execute("SELECT * FROM recurring ORDER BY next_expected").fetchall()
    return [dict(r) for r in rows]
