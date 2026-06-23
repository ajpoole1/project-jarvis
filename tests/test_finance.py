"""Unit tests for finance skill.

All tests use fixture data — no live API calls, no ~/.jarvis.env required.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Wire up imports — add repo root and skill dir so we can import directly
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[1]
_FINANCE_DIR = _REPO_ROOT / "skills" / "finance"
for _p in (str(_REPO_ROOT), str(_FINANCE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from skills.finance.alerts import (  # noqa: E402
    _check_bill_shortfall,
    _check_duplicate_charges,
    _check_large_unusual,
)
from skills.finance.brief import MORTGAGE_BUFFER_GOAL, finance_brief  # noqa: E402
from skills.finance.csv_import import detect_format, parse_csv  # noqa: E402
from skills.finance.db import (  # noqa: E402
    detect_recurring,
    get_accounts,
    get_recurring,
    get_transactions,
    init_db,
    upsert_account,
    upsert_transaction,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Isolated SQLite connection wired to a temp file."""
    conn = init_db(str(tmp_path / "finance.db"))
    yield conn
    conn.close()


@pytest.fixture()
def db_path(tmp_path):
    """Return path string for a fresh finance.db."""
    return str(tmp_path / "finance.db")


def _sample_account(overrides: dict | None = None) -> dict:
    base = {
        "id": "acct-rbc-chequing",
        "institution": "RBC",
        "name": "RBC Chequing",
        "type": "bank",
        "currency": "CAD",
        "balance_current": 2840.00,
        "owner": "personal",
        "source": "wealthica",
        "last_synced": "2026-06-17T10:00:00+00:00",
    }
    return {**base, **(overrides or {})}


def _sample_txn(overrides: dict | None = None) -> dict:
    base = {
        "id": "txn-001",
        "account_id": "acct-rbc-chequing",
        "date": "2026-06-15",
        "amount": -85.00,
        "description": "METRO GROCERIES MONTREAL",
        "category": "Groceries",
        "currency": "CAD",
        "owner": "personal",
        "is_pending": 0,
        "source": "wealthica",
        "note": None,
    }
    return {**base, **(overrides or {})}


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------


def test_init_db_creates_tables(db):
    tables = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"accounts", "transactions", "recurring", "rules", "sync_state", "goals"} <= tables


# ---------------------------------------------------------------------------
# upsert_account
# ---------------------------------------------------------------------------


def test_upsert_account_insert(db):
    upsert_account(db, _sample_account())
    rows = get_accounts(db)
    assert len(rows) == 1
    assert rows[0]["institution"] == "RBC"
    assert rows[0]["balance_current"] == 2840.00


def test_upsert_account_update_balance(db):
    upsert_account(db, _sample_account())
    upsert_account(db, _sample_account({"balance_current": 3000.00}))
    rows = get_accounts(db)
    assert len(rows) == 1
    assert rows[0]["balance_current"] == 3000.00


# ---------------------------------------------------------------------------
# upsert_transaction
# ---------------------------------------------------------------------------


def test_upsert_transaction_returns_true_for_new(db):
    assert upsert_transaction(db, _sample_txn()) is True


def test_upsert_transaction_returns_false_for_duplicate(db):
    txn = _sample_txn()
    upsert_transaction(db, txn)
    assert upsert_transaction(db, txn) is False


def test_upsert_transaction_pending_cleared(db):
    txn = _sample_txn({"is_pending": 1})
    upsert_transaction(db, txn)
    # Re-upsert as cleared
    upsert_transaction(db, _sample_txn({"is_pending": 0}))
    row = db.execute("SELECT is_pending FROM transactions WHERE id = 'txn-001'").fetchone()
    assert row["is_pending"] == 0


def test_get_transactions_filter_by_date(db):
    upsert_transaction(db, _sample_txn({"id": "t1", "date": "2026-06-10"}))
    upsert_transaction(db, _sample_txn({"id": "t2", "date": "2026-06-15"}))
    upsert_transaction(db, _sample_txn({"id": "t3", "date": "2026-06-20"}))
    results = get_transactions(db, start_date="2026-06-11", end_date="2026-06-18")
    assert len(results) == 1
    assert results[0]["id"] == "t2"


def test_get_transactions_filter_by_owner(db):
    upsert_transaction(db, _sample_txn({"id": "t1", "owner": "personal"}))
    upsert_transaction(db, _sample_txn({"id": "t2", "owner": "altaforma"}))
    personal = get_transactions(db, owner="personal")
    assert len(personal) == 1
    assert personal[0]["id"] == "t1"


# ---------------------------------------------------------------------------
# detect_recurring
# ---------------------------------------------------------------------------


def _insert_recurring_series(db, merchant: str, amounts: list[float], days_apart: int) -> None:
    base = date(2026, 3, 1)
    for i, amount in enumerate(amounts):
        d = (base + timedelta(days=i * days_apart)).isoformat()
        upsert_transaction(
            db,
            {
                "id": f"rec-{merchant}-{i}",
                "account_id": "acct-001",
                "date": d,
                "amount": -amount,
                "description": merchant,
                "category": "Entertainment",
                "currency": "CAD",
                "owner": "personal",
                "is_pending": 0,
                "source": "wealthica",
                "note": None,
            },
        )


def test_detect_recurring_monthly(db):
    _insert_recurring_series(db, "NETFLIX.COM", [17.99, 17.99, 17.99], days_apart=30)
    count = detect_recurring(db)
    assert count >= 1
    rows = get_recurring(db)
    assert len(rows) >= 1
    assert rows[0]["cadence_days"] == 30
    assert abs(rows[0]["amount_median"] - 17.99) < 0.01


def test_detect_recurring_weekly(db):
    _insert_recurring_series(db, "GYM MEMBERSHIP", [25.00, 25.00, 25.00], days_apart=7)
    detect_recurring(db)
    rows = get_recurring(db)
    weekly = [r for r in rows if r["cadence_days"] == 7]
    assert len(weekly) == 1


def test_detect_recurring_skips_irregular(db):
    """Transactions with no consistent interval should not appear."""
    upsert_transaction(
        db, _sample_txn({"id": "ir1", "date": "2026-03-01", "description": "RANDOM VENDOR"})
    )
    upsert_transaction(
        db, _sample_txn({"id": "ir2", "date": "2026-04-20", "description": "RANDOM VENDOR"})
    )
    detect_recurring(db)
    rows = [r for r in get_recurring(db) if "RANDOM" in (r["merchant_norm"] or "")]
    assert len(rows) == 0


def test_detect_recurring_sets_next_expected(db):
    _insert_recurring_series(db, "SPOTIFY.COM", [10.99, 10.99, 10.99], days_apart=30)
    detect_recurring(db)
    rows = get_recurring(db)
    assert rows[0]["next_expected"] is not None
    # next_expected should be in the future or recent past (test data uses 2026-03)
    ne = date.fromisoformat(rows[0]["next_expected"])
    ls = date.fromisoformat(rows[0]["last_seen"])
    assert (ne - ls).days == 30


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


@pytest.fixture()
def mbna_csv(tmp_path):
    path = tmp_path / "mbna.csv"
    path.write_text(
        "Date,Transaction,Name,Memo,Amount\n"
        "04/15/2026,Debit,AMAZON.CA MARKETPLACE,,84.99\n"
        "04/10/2026,Credit,PAYMENT,,-200.00\n"
        "04/05/2026,Debit,METRO GROCERIES,,55.40\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture()
def rogers_csv(tmp_path):
    path = tmp_path / "rogers.csv"
    path.write_text(
        "Transaction Date,Post Date,Description,Category,Card Number,Credit,Debit\n"
        "2026-04-15,2026-04-17,AMAZON.CA,Shopping,xxxx1234,,84.99\n"
        "2026-04-10,2026-04-10,PAYMENT,,xxxx1234,200.00,\n"
        "2026-04-05,2026-04-06,METRO GROCERIES,Groceries,xxxx1234,,55.40\n",
        encoding="utf-8",
    )
    return str(path)


def test_detect_format_mbna(mbna_csv):
    assert detect_format(mbna_csv) == "mbna"


def test_detect_format_rogers(rogers_csv):
    assert detect_format(rogers_csv) == "rogers"


def test_parse_mbna_sign_convention(mbna_csv):
    txns = parse_csv(mbna_csv, "acct-mbna")
    # purchases should be negative (money out)
    purchases = [t for t in txns if "AMAZON" in t["description"] or "METRO" in t["description"]]
    assert all(t["amount"] < 0 for t in purchases)
    # payment should be positive (money in — reduces CC balance)
    payments = [t for t in txns if "PAYMENT" in t["description"]]
    assert all(t["amount"] > 0 for t in payments)


def test_parse_rogers_sign_convention(rogers_csv):
    txns = parse_csv(rogers_csv, "acct-rogers")
    purchases = [t for t in txns if "AMAZON" in t["description"] or "METRO" in t["description"]]
    assert all(t["amount"] < 0 for t in purchases)
    payments = [t for t in txns if "PAYMENT" in t["description"]]
    assert all(t["amount"] > 0 for t in payments)


def test_csv_dedup_same_row_twice(db, mbna_csv):
    txns = parse_csv(mbna_csv, "acct-mbna")
    for txn in txns:
        txn["owner"] = "personal"
    inserted_first = sum(1 for t in txns if upsert_transaction(db, t))
    inserted_second = sum(1 for t in txns if upsert_transaction(db, {**t}))
    assert inserted_first == 3
    assert inserted_second == 0  # all duplicates


def test_csv_dedup_key_is_stable(mbna_csv):
    """Same file parsed twice produces identical ids."""
    ids_a = {t["id"] for t in parse_csv(mbna_csv, "acct-mbna")}
    ids_b = {t["id"] for t in parse_csv(mbna_csv, "acct-mbna")}
    assert ids_a == ids_b


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def _seed_accounts(db, chequing: float = 2840.00, cc: float = 1240.00) -> None:
    upsert_account(
        db, _sample_account({"id": "acct-chequing", "type": "bank", "balance_current": chequing})
    )
    upsert_account(
        db,
        {
            "id": "acct-cc",
            "institution": "TD",
            "name": "TD Visa",
            "type": "credit",
            "currency": "CAD",
            "balance_current": cc,
            "owner": "personal",
            "source": "wealthica",
            "last_synced": None,
        },
    )


def test_alert_large_unusual_new_merchant(db):
    _seed_accounts(db)
    upsert_transaction(
        db,
        _sample_txn(
            {
                "id": "t-big-001",
                "amount": -340.00,
                "description": "BEST BUY CANADA",
                "category": "Electronics",
            }
        ),
    )
    alerts = _check_large_unusual(db, ["t-big-001"])
    assert len(alerts) == 1
    assert alerts[0]["type"] == "large_unusual_charge"
    assert "new merchant" in alerts[0]["reason"]


def test_alert_large_unusual_below_threshold_no_alert(db):
    _seed_accounts(db)
    upsert_transaction(
        db,
        _sample_txn(
            {
                "id": "t-small-001",
                "amount": -150.00,
                "description": "SOME VENDOR",
            }
        ),
    )
    alerts = _check_large_unusual(db, ["t-small-001"])
    assert len(alerts) == 0


def test_alert_large_unusual_known_merchant_no_alert(db):
    """Known merchant with normal amount should not trigger."""
    _seed_accounts(db)
    # Seed 5 prior Metro transactions so it's a known merchant
    for i in range(5):
        upsert_transaction(
            db,
            _sample_txn(
                {
                    "id": f"metro-prior-{i}",
                    "amount": -145.00,
                    "description": "METRO GROCERIES",
                    "category": "Groceries",
                }
            ),
        )
    # New Metro txn within normal range
    upsert_transaction(
        db,
        _sample_txn(
            {
                "id": "metro-new",
                "amount": -155.00,
                "description": "METRO GROCERIES",
                "category": "Groceries",
            }
        ),
    )
    alerts = _check_large_unusual(db, ["metro-new"])
    assert len(alerts) == 0


def test_alert_bill_shortfall_triggered(db):
    # Very low chequing
    _seed_accounts(db, chequing=100.00)
    # Insert recurring bill due tomorrow
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    last_month = (date.today() - timedelta(days=30)).isoformat()
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("DESJARDINS MORTGAGE", 2140.00, 30, last_month, tomorrow, "personal"),
    )
    db.commit()
    alerts = _check_bill_shortfall(db)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "bill_shortfall"
    assert alerts[0]["shortfall"] > 0


def test_alert_bill_shortfall_no_alert_when_covered(db):
    _seed_accounts(db, chequing=5000.00)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    last_month = (date.today() - timedelta(days=30)).isoformat()
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("NETFLIX COM", 17.99, 30, last_month, tomorrow, "personal"),
    )
    db.commit()
    alerts = _check_bill_shortfall(db)
    assert len(alerts) == 0


def test_alert_duplicate_charge(db):
    today = date.today().isoformat()
    upsert_transaction(
        db,
        _sample_txn(
            {"id": "dup-001", "date": today, "amount": -49.99, "description": "SOME SERVICE"}
        ),
    )
    upsert_transaction(
        db,
        _sample_txn(
            {"id": "dup-002", "date": today, "amount": -49.99, "description": "SOME SERVICE"}
        ),
    )
    alerts = _check_duplicate_charges(db, ["dup-002"])
    assert len(alerts) == 1
    assert alerts[0]["type"] == "duplicate_charge"
    assert "dup-001" in alerts[0]["duplicate_ids"]


def test_alert_duplicate_no_false_positive_different_amounts(db):
    today = date.today().isoformat()
    upsert_transaction(
        db, _sample_txn({"id": "a1", "date": today, "amount": -49.99, "description": "VENDOR X"})
    )
    upsert_transaction(
        db, _sample_txn({"id": "a2", "date": today, "amount": -99.99, "description": "VENDOR X"})
    )
    alerts = _check_duplicate_charges(db, ["a2"])
    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# finance_brief()
# ---------------------------------------------------------------------------


def test_finance_brief_empty_db(tmp_path):
    """brief() on a fresh DB should return sensible zero values."""
    # brief() returns _empty when db doesn't exist yet
    result = finance_brief(db_path=str(tmp_path / "nonexistent.db"))
    assert result["liquid"] == 0.0
    assert result["buffer_gap"] == float(MORTGAGE_BUFFER_GOAL)
    assert result["data_age_hours"] is None


def test_finance_brief_with_data(tmp_path, monkeypatch):
    db_file = tmp_path / "finance.db"
    conn = init_db(str(db_file))

    upsert_account(conn, _sample_account({"balance_current": 3000.00}))
    upsert_account(
        conn,
        {
            "id": "acct-cc",
            "institution": "TD",
            "name": "TD Visa",
            "type": "credit",
            "currency": "CAD",
            "balance_current": 500.00,
            "owner": "personal",
            "source": "wealthica",
            "last_synced": None,
        },
    )

    today = date.today().isoformat()
    upsert_transaction(
        conn, _sample_txn({"date": today, "amount": -120.00, "category": "Groceries"})
    )

    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value, updated) VALUES ('last_sync', ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()

    result = finance_brief(db_path=str(db_file))
    assert result["liquid"] == pytest.approx(3000.00 - 500.00)
    assert result["cc_outstanding"] == pytest.approx(500.00)
    assert result["buffer_gap"] == pytest.approx(max(0, MORTGAGE_BUFFER_GOAL - 3000.00))
    assert result["data_age_hours"] is not None
    assert result["data_age_hours"] < 1  # just synced
    assert result["top_spend_this_week"] is not None
    assert result["top_spend_this_week"]["category"] == "Groceries"


# ---------------------------------------------------------------------------
# P1 command smoke tests (via skill.py functions directly)
# ---------------------------------------------------------------------------


def _load_skill():
    """Load skill.py functions for testing without running main()."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("finance_skill", _FINANCE_DIR / "skill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_skill = _load_skill()


@pytest.fixture()
def populated_db(tmp_path, monkeypatch):
    """DB with one account + several transactions."""
    db_file = tmp_path / "finance.db"
    monkeypatch.setattr(_skill, "DB_PATH", db_file)
    conn = init_db(str(db_file))

    upsert_account(conn, _sample_account({"balance_current": 2840.00}))
    upsert_account(
        conn,
        {
            "id": "acct-cc",
            "institution": "TD",
            "name": "TD Visa",
            "type": "credit",
            "currency": "CAD",
            "balance_current": 1240.00,
            "owner": "personal",
            "source": "wealthica",
            "last_synced": None,
        },
    )

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    month_start = date.today().replace(day=1).isoformat()

    upsert_transaction(
        conn, _sample_txn({"id": "t1", "date": today, "amount": -145.00, "category": "Groceries"})
    )
    upsert_transaction(
        conn,
        _sample_txn(
            {"id": "t2", "date": yesterday, "amount": -78.00, "category": "Transportation"}
        ),
    )
    upsert_transaction(
        conn,
        _sample_txn(
            {
                "id": "t3",
                "date": month_start,
                "amount": 2800.00,
                "category": "Income",
                "description": "PAYROLL DEPOSIT",
            }
        ),
    )
    upsert_transaction(
        conn,
        _sample_txn(
            {
                "id": "t4",
                "date": today,
                "amount": -25.00,
                "category": "Technology",
                "owner": "altaforma",
                "description": "DIGITALOCEAN",
            }
        ),
    )
    yield conn
    conn.close()


def test_cmd_accounts_output(populated_db):
    result = _skill.cmd_accounts(populated_db)
    assert "RBC" in result
    assert "Bank accounts" in result
    assert "Net liquid" in result


def test_cmd_liquid_output(populated_db):
    result = _skill.cmd_liquid(populated_db)
    assert "Net liquid" in result
    assert "Mortgage buffer goal" in result


def test_cmd_spend_output(populated_db):
    start, end = date.today().replace(day=1).isoformat(), date.today().isoformat()
    result = _skill.cmd_spend(populated_db, start, end)
    assert "Groceries" in result


def test_cmd_net_output(populated_db):
    start, end = date.today().replace(day=1).isoformat(), date.today().isoformat()
    result = _skill.cmd_net(populated_db, start, end)
    assert "In:" in result
    assert "Out:" in result
    assert "Net:" in result


def test_cmd_search_finds_match(populated_db):
    result = _skill.cmd_search(populated_db, "PAYROLL")
    assert "PAYROLL" in result


def test_cmd_search_no_match(populated_db):
    result = _skill.cmd_search(populated_db, "NONEXISTENT_VENDOR_XYZ")
    assert "No transactions matching" in result


def test_cmd_top_output(populated_db):
    start, end = date.today().replace(day=1).isoformat(), date.today().isoformat()
    result = _skill.cmd_top(populated_db, 5, start, end)
    assert "Top 5 merchants" in result


def test_cmd_tag_updates_owner(populated_db):
    result = _skill.cmd_tag(populated_db, "t1", "altaforma")
    assert "owner=altaforma" in result
    row = populated_db.execute("SELECT owner FROM transactions WHERE id = 't1'").fetchone()
    assert row["owner"] == "altaforma"


def test_cmd_tag_with_category(populated_db):
    result = _skill.cmd_tag(populated_db, "t1", "altaforma", category="Infrastructure")
    assert "category=Infrastructure" in result


def test_cmd_tag_prefix_match(populated_db):
    result = _skill.cmd_tag(populated_db, "t2", "personal")
    assert "owner=personal" in result


def test_cmd_tag_not_found(populated_db):
    result = _skill.cmd_tag(populated_db, "zzz-no-such", "personal")
    assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# WS3: tag --rule / --confirm-rule / --discard-rule
# ---------------------------------------------------------------------------


def _seed_tag_txns(db):
    """Insert several transactions with the same merchant prefix for rule-proposal tests.

    Uses realistic Netflix descriptions: stable prefix + varying date suffix so the
    derived pattern (NETFLIX.COM%) matches all 4 rows.
    """
    from skills.finance.db import upsert_transaction

    for i in range(4):
        upsert_transaction(
            db,
            {
                "id": f"ws3-{i}",
                "account_id": "rbc-04330-5118989",
                "date": f"2026-0{i + 1}-15",
                "amount": -(30.0 + i),
                "description": f"NETFLIX.COM #2026-0{i + 1}-15",
                "category": None,
                "owner": "personal",
            },
        )


def test_cmd_tag_propose_rule_writes_proposal(db):
    _seed_tag_txns(db)
    result = _skill.cmd_tag(db, "ws3-0", "personal", category="subscriptions", propose_rule=True)
    assert "Rule proposal" in result
    assert "NETFLIX.COM" in result
    assert "4" in result  # 4 matching transactions
    assert "--confirm-rule" in result
    row = db.execute("SELECT * FROM tag_proposals").fetchone()
    assert row is not None
    assert "NETFLIX" in row["match_merchant"]
    assert row["category"] == "subscriptions"
    assert row["match_count"] == 4


def test_cmd_tag_propose_rule_tags_the_one_transaction(db):
    _seed_tag_txns(db)
    _skill.cmd_tag(db, "ws3-0", "personal", category="subscriptions", propose_rule=True)
    row = db.execute("SELECT category, owner FROM transactions WHERE id = 'ws3-0'").fetchone()
    assert row["category"] == "subscriptions"
    assert row["owner"] == "personal"


def test_cmd_tag_confirm_writes_rule_and_back_applies(db):
    _seed_tag_txns(db)
    _skill.cmd_tag(db, "ws3-0", "personal", category="subscriptions", propose_rule=True)
    prop = db.execute("SELECT id FROM tag_proposals").fetchone()
    result = _skill.cmd_tag_confirm(db, prop["id"])
    assert "Rule written" in result
    assert "subscriptions" in result
    # All 4 transactions should now be categorised
    rows = db.execute("SELECT category FROM transactions WHERE id LIKE 'ws3-%'").fetchall()
    assert all(r["category"] == "subscriptions" for r in rows)
    # Proposal should be deleted
    leftover = db.execute("SELECT * FROM tag_proposals WHERE id = ?", (prop["id"],)).fetchone()
    assert leftover is None


def test_cmd_tag_confirm_not_found(db):
    result = _skill.cmd_tag_confirm(db, 9999)
    assert "not found" in result.lower()


def test_cmd_tag_discard_removes_proposal(db):
    _seed_tag_txns(db)
    _skill.cmd_tag(db, "ws3-0", "personal", category="subscriptions", propose_rule=True)
    prop = db.execute("SELECT id FROM tag_proposals").fetchone()
    result = _skill.cmd_tag_discard(db, prop["id"])
    assert "discarded" in result.lower()
    leftover = db.execute("SELECT * FROM tag_proposals WHERE id = ?", (prop["id"],)).fetchone()
    assert leftover is None
    # No proposal-derived rule should have been written (seed rule for NETFLIX% is pre-existing)
    rules = db.execute(
        "SELECT * FROM finance_rules WHERE match_merchant = 'NETFLIX.COM%'"
    ).fetchall()
    assert len(rules) == 0


def test_cmd_tag_discard_not_found(db):
    result = _skill.cmd_tag_discard(db, 9999)
    assert "not found" in result.lower()


def test_cmd_tag_without_rule_flag_no_proposal(db):
    _seed_tag_txns(db)
    result = _skill.cmd_tag(db, "ws3-0", "personal", category="subscriptions")
    assert "Rule proposal" not in result
    count = db.execute("SELECT COUNT(*) FROM tag_proposals").fetchone()[0]
    assert count == 0


def test_cmd_bills_due_no_bills(populated_db):
    result = _skill.cmd_bills_due(populated_db, days=7)
    assert "No bills due" in result


def test_cmd_recurring_no_data(populated_db):
    result = _skill.cmd_recurring(populated_db)
    assert "No recurring" in result


# ---------------------------------------------------------------------------
# Tom QA regression tests (blocking bugs from PR #74 review)
# ---------------------------------------------------------------------------


def test_apply_finance_rules_handles_more_than_999_ids(db):
    """apply_finance_rules must not raise sqlite3.OperationalError for >999 txn_ids."""
    from skills.finance.db import apply_finance_rules

    for i in range(1001):
        upsert_transaction(
            db,
            {
                "id": f"bulk-{i:04d}",
                "account_id": "rbc-04330-5118989",
                "date": "2026-01-15",
                "amount": -10.0,
                "description": "IGA SUPERMARCHE",
                "category": None,
                "owner": "personal",
            },
        )
    # Should complete without error; IGA% seed rule will match all rows
    updated = apply_finance_rules(db, [f"bulk-{i:04d}" for i in range(1001)])
    assert updated >= 0


def test_upsert_finance_rule_is_transfer_none_does_not_crash(db):
    """upsert_finance_rule must handle is_transfer=None without TypeError."""
    from skills.finance.db import upsert_finance_rule

    rule_id = upsert_finance_rule(
        db,
        {
            "match_merchant": "%TEST_NONE%",
            "category": "test",
            "owner": "personal",
            "is_transfer": None,
        },
    )
    assert isinstance(rule_id, int)
    row = db.execute("SELECT is_transfer FROM finance_rules WHERE id = ?", (rule_id,)).fetchone()
    assert row["is_transfer"] == 0


def test_migrate_works_without_row_factory(tmp_path):
    """_migrate must not crash when called on a connection without row_factory set."""
    import sqlite3 as _sqlite3

    from skills.finance.db import _DDL, _migrate

    db_file = str(tmp_path / "raw.db")
    conn = _sqlite3.connect(db_file)
    # Do NOT set row_factory — raw tuple rows
    conn.executescript(_DDL)
    # Seed a legacy rules row to trigger the migration path
    conn.execute(
        "INSERT INTO rules (pattern, owner, category, created) VALUES (?, ?, ?, ?)",
        ("%RAW_TEST%", "personal", "groceries", "2026-01-01"),
    )
    conn.commit()
    # Must not raise TypeError
    _migrate(conn)
    conn.commit()
    migrated = conn.execute("SELECT COUNT(*) FROM finance_rules").fetchone()[0]
    assert migrated == 1
    conn.close()


def test_match_finance_rule_bracket_in_description(db):
    """_match_finance_rule must match [POS] literally, not as a glob character class."""
    from skills.finance.db import _match_finance_rule

    rule = {"match_merchant": "%[POS]%", "owner": "personal", "category": "pos_purchase"}
    txn_match = {"description": "INTERAC [POS] SOME STORE", "amount": -25.0}
    txn_no_match = {"description": "INTERAC SOME STORE", "amount": -25.0}
    assert _match_finance_rule(rule, txn_match) is True
    assert _match_finance_rule(rule, txn_no_match) is False


# ---------------------------------------------------------------------------
# rule add / list / remove
# ---------------------------------------------------------------------------


def test_rule_set_with_category(populated_db):
    result = _skill.cmd_rule_set(
        populated_db, "%DIGITALOCEAN%", category="infrastructure", owner="altaforma"
    )
    assert "Rule set" in result
    assert "id=" in result
    assert "%DIGITALOCEAN%" in result
    assert "altaforma" in result
    assert "infrastructure" in result
    assert "Applied to" in result
    row = populated_db.execute(
        "SELECT * FROM finance_rules WHERE match_merchant = '%DIGITALOCEAN%'"
    ).fetchone()
    assert row is not None
    assert row["owner"] == "altaforma"
    assert row["category"] == "infrastructure"


def test_rule_set_without_category(populated_db):
    result = _skill.cmd_rule_set(populated_db, "%SHOPIFY%", owner="altaforma")
    assert "Rule set" in result
    assert "Applied to" in result
    row = populated_db.execute(
        "SELECT * FROM finance_rules WHERE match_merchant = '%SHOPIFY%'"
    ).fetchone()
    assert row is not None
    assert row["category"] is None


def test_rule_set_applies_to_existing(db):
    upsert_transaction(
        db,
        _sample_txn({"id": "r1", "description": "DIGITALOCEAN INVOICE", "owner": "personal"}),
    )
    upsert_transaction(
        db,
        _sample_txn({"id": "r2", "description": "METRO GROCERIES", "owner": "personal"}),
    )
    result = _skill.cmd_rule_set(db, "%DIGITALOCEAN%", owner="altaforma", category="hosting")
    assert "Applied to" in result
    row = db.execute("SELECT owner, category FROM transactions WHERE id = 'r1'").fetchone()
    assert row["owner"] == "altaforma"
    assert row["category"] == "hosting"


def test_rule_list_shows_rows(db):
    _skill.cmd_rule_set(db, "%SHOPIFY%", owner="altaforma", category="saas")
    result = _skill.cmd_rule_list(db)
    assert "%SHOPIFY%" in result
    assert "altaforma" in result
    assert "saas" in result


def test_rule_list_empty(db):
    db.execute("DELETE FROM finance_rules")
    db.commit()
    result = _skill.cmd_rule_list(db)
    assert "No finance rules" in result


def test_rule_rm_deletes_row(db):
    _skill.cmd_rule_set(db, "%DIGITALOCEAN%", owner="altaforma", category="infrastructure")
    row = db.execute(
        "SELECT id FROM finance_rules WHERE match_merchant = '%DIGITALOCEAN%'"
    ).fetchone()
    rule_id = row["id"]
    result = _skill.cmd_rule_rm(db, rule_id)
    assert f"Rule {rule_id} removed" in result
    assert "%DIGITALOCEAN%" in result
    remaining = db.execute("SELECT id FROM finance_rules WHERE id = ?", (rule_id,)).fetchone()
    assert remaining is None


def test_rule_rm_nonexistent(db):
    result = _skill.cmd_rule_rm(db, 999)
    assert "not found" in result


def test_rule_set_empty_merchant(db):
    count_before = db.execute("SELECT COUNT(*) FROM finance_rules").fetchone()[0]
    result = _skill.cmd_rule_set(db, "")
    assert "Error" in result
    assert "non-empty" in result
    count_after = db.execute("SELECT COUNT(*) FROM finance_rules").fetchone()[0]
    assert count_after == count_before


def test_rule_set_invalid_owner(db):
    count_before = db.execute("SELECT COUNT(*) FROM finance_rules").fetchone()[0]
    result = _skill.cmd_rule_set(db, "%AMAZON%", owner="business")
    assert "Error" in result
    assert "personal" in result or "altaforma" in result
    count_after = db.execute("SELECT COUNT(*) FROM finance_rules").fetchone()[0]
    assert count_after == count_before


# ---------------------------------------------------------------------------
# finance_rules — mixed-merchant FX-drift proof (Anthropic Max sub vs API usage)
# ---------------------------------------------------------------------------
# Seeded rules (from _seed_transfer_rules):
#   priority=20: ANTHROPIC%CLAUDE% + recurrence=fixed_monthly → personal/subscriptions
#   priority=10: ANTHROPIC%            + recurrence=variable   → altaforma/subscriptions
#
# The key invariant: the sub rule matches on descriptor+recurrence, NOT amount.
# Two cycles with different converted CAD amounts must both hit personal/subscriptions.


def test_anthropic_sub_matches_personal_across_fx_cycles(db):
    """Claude Max sub (fixed_monthly) → personal regardless of converted CAD amount."""
    # Cycle 1: USD/CAD = 1.36 → $140 × 1.36 = ~$190.40
    t1 = _sample_txn(
        {
            "id": "ant-sub-1",
            "description": "ANTHROPIC CLAUDE AI SUBSCRIPTION",
            "amount": -190.40,
        }
    )
    # Cycle 2: USD/CAD = 1.42 → $140 × 1.42 = ~$198.80
    t2 = _sample_txn(
        {
            "id": "ant-sub-2",
            "description": "ANTHROPIC CLAUDE AI SUBSCRIPTION",
            "amount": -198.80,
        }
    )
    upsert_transaction(db, t1)
    upsert_transaction(db, t2)
    from skills.finance.db import apply_finance_rules

    apply_finance_rules(db, ["ant-sub-1", "ant-sub-2"])
    rows = db.execute(
        "SELECT id, owner, category FROM transactions WHERE id IN ('ant-sub-1', 'ant-sub-2')"
        " ORDER BY id"
    ).fetchall()
    assert rows[0]["owner"] == "personal"
    assert rows[1]["owner"] == "personal"
    assert rows[0]["category"] == "subscriptions"


def test_anthropic_api_matches_altaforma(db):
    """Anthropic API usage (no 'CLAUDE' in description) → altaforma."""
    t = _sample_txn(
        {
            "id": "ant-api-1",
            "description": "ANTHROPIC USAGE BILLING",
            "amount": -47.83,
        }
    )
    upsert_transaction(db, t)
    from skills.finance.db import apply_finance_rules

    apply_finance_rules(db, ["ant-api-1"])
    row = db.execute("SELECT owner, category FROM transactions WHERE id = 'ant-api-1'").fetchone()
    assert row["owner"] == "altaforma"
    assert row["category"] == "subscriptions"


# ---------------------------------------------------------------------------
# WS2: ingest (dropbox sweep)
# ---------------------------------------------------------------------------


def _write_rbc_csv(path) -> None:
    """Write a minimal valid RBC CSV to path."""
    path.write_text(
        "Account Type,Account Number,Transaction Date,Cheque Number,Description 1,"
        "Description 2,CAD$,USD$\n"
        "Chequing,5118989,6/10/2026,,METRO GROCERIES,,-55.40,\n"
        "Chequing,5118989,6/12/2026,,PAYROLL DEPOSIT,,+2800.00,\n",
        encoding="utf-8-sig",
    )


def test_ingest_happy_path_rbc(tmp_path, monkeypatch):
    """RBC CSV in inbox → imported, archived, no file left in inbox."""
    inbox = tmp_path / "inbox"
    archive = inbox / "archive"
    inbox.mkdir()

    _write_rbc_csv(inbox / "rbc-chequing-june.csv")

    conn = init_db(str(tmp_path / "finance.db"))
    monkeypatch.setattr(_skill, "FINANCE_INBOX_DIR", inbox)

    result = _skill.cmd_ingest(conn, post_discord=False)

    assert "OK" in result
    assert "rbc-chequing-june.csv" in result
    assert not (inbox / "rbc-chequing-june.csv").exists()
    archived = list(archive.glob("*rbc-chequing-june.csv"))
    assert len(archived) == 1
    conn.close()


def test_ingest_unidentified_goes_to_failed(tmp_path, monkeypatch):
    """Non-CSV-format file → moved to failed/, not imported."""
    inbox = tmp_path / "inbox"
    failed = inbox / "failed"
    inbox.mkdir()

    garbage = inbox / "mystery.csv"
    garbage.write_text("this,is,not,a,bank,export\nrow1,row2,row3,row4,row5,row6\n")

    conn = init_db(str(tmp_path / "finance.db"))
    monkeypatch.setattr(_skill, "FINANCE_INBOX_DIR", inbox)

    result = _skill.cmd_ingest(conn, post_discord=False)

    assert "FAILED" in result
    assert (failed / "mystery.csv").exists()
    assert not garbage.exists()
    conn.close()


def test_ingest_td_without_account_in_filename(tmp_path, monkeypatch):
    """TD file without account ID in filename → quarantined to failed/."""
    inbox = tmp_path / "inbox"
    failed = inbox / "failed"
    inbox.mkdir()

    # A valid TD bank file (5-col, YYYY-MM-DD)
    td_file = inbox / "statement-june.csv"
    td_file.write_text(
        "2026-06-10,PAYROLL DEPOSIT,+2800.00,,5000.00\n" "2026-06-12,TIM HORTONS,-4.75,,4995.25\n"
    )

    conn = init_db(str(tmp_path / "finance.db"))
    monkeypatch.setattr(_skill, "FINANCE_INBOX_DIR", inbox)

    result = _skill.cmd_ingest(conn, post_discord=False)

    assert "FAILED" in result
    assert (failed / "statement-june.csv").exists()
    conn.close()


def test_ingest_empty_inbox(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    conn = init_db(str(tmp_path / "finance.db"))
    monkeypatch.setattr(_skill, "FINANCE_INBOX_DIR", inbox)
    result = _skill.cmd_ingest(conn, post_discord=False)
    assert "empty" in result.lower()
    conn.close()


def test_ingest_idempotent(tmp_path, monkeypatch):
    """Running ingest twice on the same file only imports once (dedup)."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_rbc_csv(inbox / "rbc-chequing-june.csv")

    conn = init_db(str(tmp_path / "finance.db"))
    monkeypatch.setattr(_skill, "FINANCE_INBOX_DIR", inbox)

    _skill.cmd_ingest(conn, post_discord=False)

    # Second drop: same file re-appears in inbox
    _write_rbc_csv(inbox / "rbc-chequing-june.csv")
    result2 = _skill.cmd_ingest(conn, post_discord=False)

    # Should report all as duplicates, not new inserts
    assert "OK" in result2
    row_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert row_count == 2  # only original 2 rows exist
    conn.close()


# ---------------------------------------------------------------------------
# P2 intelligence layer — compare, runway, altaforma, subs-audit, coda helpers
# ---------------------------------------------------------------------------


def _insert_month_spend(db, year: int, month: int, categories: dict[str, float]) -> None:
    """Insert spend transactions for a given calendar month."""
    day = 15
    for i, (cat, amount) in enumerate(categories.items()):
        upsert_transaction(
            db,
            {
                "id": f"hist-{year}-{month:02d}-{i}",
                "account_id": "acct-rbc-chequing",
                "date": f"{year}-{month:02d}-{day:02d}",
                "amount": -abs(amount),
                "description": f"VENDOR {cat.upper()}",
                "category": cat,
                "currency": "CAD",
                "owner": "personal",
                "is_pending": 0,
                "source": "csv_import",
                "note": None,
            },
        )


def test_compare_month(db):
    """compare --month returns table with Period/Baseline footer."""
    today = date.today()
    # Seed 3 prior months with >=5 transactions each (MIN_TXNS=5)
    for delta in range(1, 4):
        m = today.month - delta
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        _insert_month_spend(
            db,
            y,
            m,
            {
                "groceries": 300.0,
                "dining": 80.0,
                "utilities": 120.0,
                "transportation": 75.0,
                "healthcare": 50.0,
                "entertainment": 30.0,
            },
        )

    # Current month spend
    upsert_transaction(
        db,
        _sample_txn(
            {
                "id": "curr-groceries",
                "date": today.replace(day=1).isoformat(),
                "amount": -500.0,
                "category": "groceries",
            }
        ),
    )

    start = today.replace(day=1).isoformat()
    result = _skill.cmd_compare(db, start, today.isoformat(), baseline_months=3)
    assert "Spend comparison (month)" in result
    assert "groceries" in result
    assert "Period:" in result
    assert "Baseline:" in result


def test_compare_week(db):
    """compare --week returns week-mode table with week baseline label."""
    today = date.today()
    week_monday = today - timedelta(days=today.weekday())

    # Seed 4 prior weeks with enough transactions
    for i in range(1, 5):
        ws = week_monday - timedelta(weeks=i)
        for j in range(3):
            upsert_transaction(
                db,
                {
                    "id": f"wk-{i}-{j}",
                    "account_id": "acct-rbc-chequing",
                    "date": (ws + timedelta(days=j)).isoformat(),
                    "amount": -50.0,
                    "description": f"VENDOR GROCERIES WK{i}",
                    "category": "groceries",
                    "currency": "CAD",
                    "owner": "personal",
                    "is_pending": 0,
                    "source": "csv_import",
                    "note": None,
                },
            )

    # Current week spend
    upsert_transaction(
        db,
        _sample_txn(
            {
                "id": "curr-wk",
                "date": week_monday.isoformat(),
                "amount": -200.0,
                "category": "groceries",
            }
        ),
    )

    result = _skill.cmd_compare(
        db, week_monday.isoformat(), today.isoformat(), baseline_months=8, week_mode=True
    )
    assert "Spend comparison (week)" in result
    assert "groceries" in result
    assert "week median" in result


def test_compare_no_history(db):
    """compare with no baseline history returns guidance message."""
    today = date.today()
    start = today.replace(day=1).isoformat()
    result = _skill.cmd_compare(db, start, today.isoformat())
    assert "Not enough history" in result or "No spend data" in result


def test_runway(db):
    """runway returns headline with months and top obligations."""
    # Seed account balances
    upsert_account(db, _sample_account({"balance_current": 5000.0}))
    # Seed a monthly recurring charge
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DESJARDINS MORTGAGE", 2140.00, 30, "2026-06-01", "2026-07-01", "personal", "mortgage"),
    )
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("HYDRO QUEBEC", 200.00, 30, "2026-06-01", "2026-07-01", "personal", "utilities"),
    )
    db.commit()

    result = _skill.cmd_runway(db)
    assert "Runway:" in result
    assert "months" in result
    assert "Liquid:" in result
    assert "Monthly obligations:" in result
    assert "DESJARDINS MORTGAGE" in result


def test_runway_include_inheritance(db):
    """--include-inheritance adds $1,300 to liquid."""
    upsert_account(db, _sample_account({"balance_current": 2000.0}))
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("NETFLIX", 18.00, 30, "2026-06-01", "2026-07-01", "personal", "subscriptions"),
    )
    db.commit()

    base = _skill.cmd_runway(db, include_inheritance=False)
    with_inh = _skill.cmd_runway(db, include_inheritance=True)
    assert "$1,300 inheritance" in with_inh

    # Runway with inheritance should be numerically higher
    def _extract_runway(text: str) -> float:
        for word in text.split():
            word = word.rstrip("m")
            try:
                return float(word)
            except ValueError:
                pass
        return 0.0

    assert _extract_runway(with_inh) > _extract_runway(base)


def test_altaforma(db):
    """altaforma returns spend grouped by category with total."""
    today = date.today()
    upsert_transaction(
        db,
        {
            "id": "alta-1",
            "account_id": "acct-rbc-chequing",
            "date": today.isoformat(),
            "amount": -45.00,
            "description": "DIGITALOCEAN",
            "category": "hosting",
            "currency": "CAD",
            "owner": "altaforma",
            "is_pending": 0,
            "source": "csv_import",
            "note": None,
        },
    )
    upsert_transaction(
        db,
        {
            "id": "alta-2",
            "account_id": "acct-rbc-chequing",
            "date": today.isoformat(),
            "amount": -20.00,
            "description": "ANTHROPIC",
            "category": "subscriptions",
            "currency": "CAD",
            "owner": "altaforma",
            "is_pending": 0,
            "source": "csv_import",
            "note": None,
        },
    )

    result = _skill.cmd_altaforma(db, quarter=False)
    assert "Altaforma" in result
    assert "hosting" in result or "subscriptions" in result
    assert "Total spend" in result
    assert "Period:" in result


def test_subs_audit_stable(db):
    """subs_audit marks regular monthly recurring as stable."""
    today = date.today()
    last_month = (today - timedelta(days=30)).isoformat()
    next_month = (today + timedelta(days=30)).isoformat()
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("NETFLIX", 17.99, 30, last_month, next_month, "personal"),
    )
    db.commit()

    result = _skill.cmd_subs_audit(db)
    assert "Stable: 1" in result
    assert "NEW (0)" in result
    assert "MISSING (0)" in result


def test_subs_audit_new(db):
    """subs_audit flags recently-seen item with no consistent cadence as NEW."""
    today = date.today()
    last_week = (today - timedelta(days=10)).isoformat()
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("NEW SERVICE", 9.99, None, last_week, None, "personal"),
    )
    db.commit()

    result = _skill.cmd_subs_audit(db)
    assert "NEW (1)" in result
    assert "NEW SERVICE" in result


def test_weekly_coda_returns_string(tmp_path, monkeypatch):
    """finance_weekly_coda() returns a string when there is altaforma spend this week."""
    db_file = tmp_path / "finance.db"
    monkeypatch.setattr(_skill, "DB_PATH", db_file)
    conn = init_db(str(db_file))

    today = date.today()
    week_monday = today - timedelta(days=today.weekday())
    conn.execute(
        """INSERT INTO transactions
           (id, account_id, date, amount, description, category, currency, owner, is_pending, source, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "alta-wk-1",
            "acct-test",
            week_monday.isoformat(),
            -75.0,
            "DIGITALOCEAN",
            "hosting",
            "CAD",
            "altaforma",
            0,
            "csv_import",
            None,
        ),
    )
    conn.commit()
    conn.close()

    result = _skill.finance_weekly_coda()
    assert result is not None
    assert isinstance(result, str)
    assert len(result) > 0


def test_monthly_coda_returns_string(tmp_path, monkeypatch):
    """finance_monthly_coda() returns a 3-line string with prior-month net."""
    db_file = tmp_path / "finance.db"
    monkeypatch.setattr(_skill, "DB_PATH", db_file)
    conn = init_db(str(db_file))

    today = date.today()
    pm_end = today.replace(day=1) - timedelta(days=1)

    conn.execute(
        """INSERT INTO transactions
           (id, account_id, date, amount, description, category, currency, owner, is_pending, source, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "pm-txn-1",
            "acct-test",
            pm_end.isoformat(),
            -200.0,
            "METRO",
            "groceries",
            "CAD",
            "personal",
            0,
            "csv_import",
            None,
        ),
    )
    conn.commit()
    conn.close()

    result = _skill.finance_monthly_coda()
    assert result is not None
    assert isinstance(result, str)
    lines = result.splitlines()
    assert len(lines) == 3
    assert "net:" in lines[0]
    assert "Subs:" in lines[1]
    assert "GST" in lines[2]


# ---------------------------------------------------------------------------
# WS4: salience block
# ---------------------------------------------------------------------------


from skills.finance.salience import (  # noqa: E402
    _cashflow_projection,
    _category_anomalies,
    _duplicate_charges,
    _large_charge_forecast,
    _subscription_drift,
    compute_salience_block,
)


def _seed_salience_db(db):
    """Seed a DB with enough data to exercise all 5 salience signals."""
    today = date.today()

    # Account with a known balance
    upsert_account(
        db,
        {
            "id": "sal-chq",
            "institution": "RBC",
            "name": "Chequing",
            "type": "bank",
            "currency": "CAD",
            "balance_current": 3000.0,
            "owner": "personal",
            "source": "csv_import",
            "last_synced": None,
        },
    )

    # 3 months of income + spend for cashflow / anomaly signals.
    # Use the 15th of each prior calendar month so these rows fall in the baseline window
    # (not in the current-month window queried by _category_anomalies).
    def _prior_month_15(n_months_ago: int) -> str:
        y, m = today.year, today.month
        for _ in range(n_months_ago):
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        return f"{y:04d}-{m:02d}-15"

    for months_ago in range(1, 4):
        day = _prior_month_15(months_ago)
        upsert_transaction(
            db,
            {
                "id": f"sal-income-{months_ago}",
                "account_id": "sal-chq",
                "date": day,
                "amount": 5000.0,
                "description": "PAYROLL DEPOSIT",
                "category": "income",
                "owner": "personal",
            },
        )
        # 5 grocery transactions per prior month so the month qualifies as a baseline period
        for j in range(5):
            upsert_transaction(
                db,
                {
                    "id": f"sal-groceries-{months_ago}-{j}",
                    "account_id": "sal-chq",
                    "date": day,
                    "amount": -60.0,
                    "description": "IGA SUPERMARCHE",
                    "category": "groceries",
                    "owner": "personal",
                },
            )

    # This month: anomalously high groceries (baseline ≈ $300/mo; this month $800 = >160% above)
    upsert_transaction(
        db,
        {
            "id": "sal-groceries-high",
            "account_id": "sal-chq",
            "date": today.isoformat(),
            "amount": -800.0,
            "description": "IGA SUPERMARCHE",
            "category": "groceries",
            "owner": "personal",
        },
    )

    # Duplicate charge pair
    upsert_transaction(
        db,
        {
            "id": "sal-dup-a",
            "account_id": "sal-chq",
            "date": (today - timedelta(days=1)).isoformat(),
            "amount": -49.99,
            "description": "AMAZON PRIME",
            "category": "subscriptions",
            "owner": "personal",
        },
    )
    upsert_transaction(
        db,
        {
            "id": "sal-dup-b",
            "account_id": "sal-chq",
            "date": today.isoformat(),
            "amount": -49.99,
            "description": "AMAZON PRIME",
            "category": "subscriptions",
            "owner": "personal",
        },
    )

    # Recurring monthly obligation
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "DESJARDINS MORTGAGE",
            3202.0,
            30,
            (today - timedelta(days=10)).isoformat(),
            (today + timedelta(days=20)).isoformat(),
            "personal",
        ),
    )

    # Irregular large charge due soon (quarterly/annual)
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "INSURANCE ANNUAL",
            750.0,
            365,
            (today - timedelta(days=300)).isoformat(),
            (today + timedelta(days=45)).isoformat(),
            "personal",
        ),
    )

    # New subscription (no cadence, seen recently)
    db.execute(
        "INSERT INTO recurring (merchant_norm, amount_median, cadence_days, last_seen, next_expected, owner) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "BRAND NEW SERVICE",
            9.99,
            None,
            (today - timedelta(days=5)).isoformat(),
            None,
            "personal",
        ),
    )
    db.commit()


def test_cashflow_projection_returns_dict(db):
    _seed_salience_db(db)
    result = _cashflow_projection(db)
    assert "monthly_income_est" in result
    assert "monthly_obligations_est" in result
    assert "monthly_net_est" in result
    assert result["monthly_income_est"] > 0
    assert result["monthly_obligations_est"] > 0


def test_large_charge_forecast_finds_annual(db):
    _seed_salience_db(db)
    result = _large_charge_forecast(db, horizon_days=60)
    assert len(result) == 1
    assert result[0]["merchant"] == "INSURANCE ANNUAL"
    assert result[0]["amount"] == 750.0
    assert result[0]["days_away"] <= 60


def test_category_anomalies_flags_high_groceries(db):
    _seed_salience_db(db)
    anomalies = _category_anomalies(db)
    cats = [a["category"] for a in anomalies]
    assert "groceries" in cats
    groc = next(a for a in anomalies if a["category"] == "groceries")
    assert groc["delta_pct"] > 20


def test_subscription_drift_flags_new(db):
    _seed_salience_db(db)
    result = _subscription_drift(db)
    assert isinstance(result["new"], list)
    assert any(item["merchant"] == "BRAND NEW SERVICE" for item in result["new"])
    assert result["alert"] is True


def test_duplicate_charges_finds_amazon_prime(db):
    _seed_salience_db(db)
    dups = _duplicate_charges(db)
    assert len(dups) >= 1
    assert any(d["description"] == "AMAZON PRIME" for d in dups)


def test_compute_salience_block_returns_all_keys(db):
    _seed_salience_db(db)
    block = compute_salience_block(db)
    assert "cashflow_projection" in block
    assert "large_charge_forecast" in block
    assert "category_anomalies" in block
    assert "subscription_drift" in block
    assert "duplicate_charges" in block


def test_compute_salience_block_empty_db(db):
    block = compute_salience_block(db)
    assert block["cashflow_projection"] is not None
    assert block["large_charge_forecast"] == []
    assert block["category_anomalies"] == []
    assert block["duplicate_charges"] == []


def test_finance_brief_includes_salience_key(tmp_path):
    """finance_brief() dict always includes salience_block key."""
    from skills.finance.brief import finance_brief

    result = finance_brief(db_path=str(tmp_path / "finance.db"))
    assert "salience_block" in result
