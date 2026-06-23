"""§10 Acceptance tests for 2026-0041 gmail redesign.

Covers:
  AC1  Daycare fixture — same sender, three dispositions
  AC2  Inbox = action queue — confirm never lands there
  AC3  Mixed-sender fixture — same sender, two types → two dispositions
  AC4  Reversible-only autonomy boundary
  AC5  Feedback loop — quarantined item dragged back surfaces correction
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Mock heavy third-party deps before importing the skill module.
# ---------------------------------------------------------------------------

_MOCK_PKGS = (
    "anthropic",
    "dotenv",
    "google",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.credentials",
    "google_auth_oauthlib",
    "google_auth_oauthlib.flow",
    "googleapiclient",
    "googleapiclient.discovery",
)
for _pkg in _MOCK_PKGS:
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "gmail-cleanup" / "skill.py"
_MOD_NAME = "gmail_cleanup_skill_redesign"
if _MOD_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _SKILL_PATH)
    skill = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = skill
    _spec.loader.exec_module(skill)
else:
    skill = sys.modules[_MOD_NAME]


# ---------------------------------------------------------------------------
# Test DB helpers
# ---------------------------------------------------------------------------


def _make_con() -> sqlite3.Connection:
    """Return an in-memory connection with all gmail tables seeded."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS gmail_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            definition TEXT NOT NULL,
            rule_type TEXT NOT NULL DEFAULT 'llm-criteria',
            rule_spec TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gmail_sender_rules (
            sender_email TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            confirmed INTEGER NOT NULL DEFAULT 0,
            last_applied TEXT
        );
        CREATE TABLE IF NOT EXISTS gmail_senders (
            sender_pattern TEXT PRIMARY KEY,
            friendly_name TEXT NOT NULL DEFAULT '',
            default_tier TEXT NOT NULL DEFAULT 'archive',
            bypass TEXT,
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS gmail_type_rules (
            type TEXT PRIMARY KEY,
            tier TEXT NOT NULL,
            disposition TEXT NOT NULL,
            needs_aj INTEGER NOT NULL DEFAULT 0,
            ping INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS gmail_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS gmail_ping_rules (
            match TEXT PRIMARY KEY,
            ping INTEGER NOT NULL DEFAULT 1,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS gmail_heartbeat_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gmail_flagged (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT NOT NULL UNIQUE,
            sender_email TEXT NOT NULL DEFAULT '',
            sender TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            reason TEXT,
            flagged_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    skill._seed_decision_layer(con)
    for name, defn in [
        ("receipts", "Receipts"),
        ("none", "Transient"),
        ("other", "Catch-all"),
        ("security", "Security"),
        ("bulletin", "Bulletin"),
        ("daycare", "Daycare"),
        ("personal", "Personal"),
    ]:
        con.execute(
            "INSERT OR IGNORE INTO gmail_tags (name, definition, created_at) VALUES (?, ?, '2026-01-01')",
            (name, defn),
        )
    con.commit()
    return con


def _email(msg_id: str, sender: str, subject: str) -> dict:
    return {
        "id": msg_id,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ]
        },
        "snippet": subject,
        "labelIds": [],
    }


def _classifier_response(*items: dict) -> MagicMock:
    """Build a mock Anthropic response returning the given per-message dicts."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=json.dumps(list(items)))]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    return mock_client


def _cls(
    type_: str,
    tier: str,
    disposition: str,
    *,
    needs_aj: bool = False,
    confidence: float = 0.95,
    uncertain: bool = False,
) -> dict:
    return {
        "type": type_,
        "tier": tier,
        "disposition": disposition,
        "needs_aj": needs_aj,
        "confidence": confidence,
        "calendar_hint": False,
        "uncertain": uncertain,
        "reason": f"test:{type_}",
        "tag": "none",
    }


# ---------------------------------------------------------------------------
# AC1 — Daycare fixture: one sender, three distinct dispositions
# ---------------------------------------------------------------------------


class TestDaycareFixture:
    """Petit Parchemin (daycare): same sender produces three distinct dispositions."""

    def _run_classify(self, emails, classifier_items, con):
        mock_client = _classifier_response(*classifier_items)
        with (
            patch.object(skill, "anthropic") as mock_anthropic,
            patch.object(skill, "fetch_calendar_context", return_value=""),
        ):
            mock_anthropic.Anthropic.return_value = mock_client
            return skill.classify_emails(emails, con)

    def test_daily_journal_bord_is_archive_trash_direct(self):
        """Daily journal de bord → archive tier, trash_direct disposition."""
        con = _make_con()
        emails = [_email("m1", "app@petitparchemin.com", "Journal de bord — Ellie 2026-06-22")]
        cls_items = [_cls("daycare_routine", "archive", "trash_direct", confidence=0.97)]
        results = self._run_classify(emails, cls_items, con)

        assert results[0].tier == "archive", f"Expected archive, got {results[0].tier}"
        assert (
            results[0].disposition == "trash_direct"
        ), f"Expected trash_direct, got {results[0].disposition}"
        assert not results[0].needs_aj

    def test_monthly_bulletin_is_aware_file(self):
        """Monthly bulletin from daycare → aware tier, file disposition."""
        con = _make_con()
        emails = [_email("m2", "app@petitparchemin.com", "Bulletin de juin 2026")]
        cls_items = [_cls("bulletin", "aware", "file", confidence=0.93)]
        results = self._run_classify(emails, cls_items, con)

        assert results[0].tier == "aware", f"Expected aware, got {results[0].tier}"
        assert results[0].disposition == "file", f"Expected file, got {results[0].disposition}"
        assert not results[0].needs_aj

    def test_staff_message_is_act_inbox_ping(self):
        """Staff message / incident report → act tier, inbox, needs_aj, ping."""
        con = _make_con()
        emails = [_email("m3", "staff@petitparchemin.com", "Incident report — Ellie today")]
        cls_items = [_cls("daycare_message", "act", "inbox", needs_aj=True, confidence=0.99)]
        results = self._run_classify(emails, cls_items, con)

        assert results[0].tier == "act", f"Expected act, got {results[0].tier}"
        assert results[0].disposition == "inbox", f"Expected inbox, got {results[0].disposition}"
        assert results[0].needs_aj

    def test_same_sender_three_different_dispositions(self):
        """The three daycare message types produce three different dispositions."""
        con = _make_con()
        emails = [
            _email("m1", "app@petitparchemin.com", "Journal de bord — Ellie"),
            _email("m2", "app@petitparchemin.com", "Bulletin de juin"),
            _email("m3", "app@petitparchemin.com", "Message from staff"),
        ]
        cls_items = [
            _cls("daycare_routine", "archive", "trash_direct"),
            _cls("bulletin", "aware", "file"),
            _cls("daycare_message", "act", "inbox", needs_aj=True),
        ]
        results = self._run_classify(emails, cls_items, con)

        dispositions = {r.msg_id: r.disposition for r in results}
        assert dispositions["m1"] == "trash_direct"
        assert dispositions["m2"] == "file"
        assert dispositions["m3"] == "inbox"


# ---------------------------------------------------------------------------
# AC2 — Inbox = action queue
# ---------------------------------------------------------------------------


class TestInboxIsActionQueue:
    """Inbox holds only items that need AJ. Confirmations never land there."""

    def _run_classify(self, emails, classifier_items, con):
        mock_client = _classifier_response(*classifier_items)
        with (
            patch.object(skill, "anthropic") as mock_anthropic,
            patch.object(skill, "fetch_calendar_context", return_value=""),
        ):
            mock_anthropic.Anthropic.return_value = mock_client
            return skill.classify_emails(emails, con)

    def test_amazon_confirmation_never_reaches_inbox(self):
        """Purchase confirmation → receipt type → archive/file, not inbox."""
        con = _make_con()
        emails = [_email("m1", "ship@amazon.ca", "Your order has shipped")]
        cls_items = [_cls("receipt", "archive", "file", confidence=0.98)]
        results = self._run_classify(emails, cls_items, con)

        assert results[0].disposition != "inbox", "Purchase confirmation must never reach inbox"
        assert results[0].tier == "archive"

    def test_unpaid_invoice_reaches_inbox_no_ping(self):
        """Unpaid invoice → act/inbox, needs_aj, no ping per gmail_type_rules."""
        con = _make_con()
        emails = [_email("m1", "billing@therapist.ca", "Invoice #42 — balance due")]
        cls_items = [_cls("unpaid_invoice", "act", "inbox", needs_aj=True, confidence=0.96)]
        results = self._run_classify(emails, cls_items, con)

        assert results[0].disposition == "inbox", "Unpaid invoice must reach inbox"
        assert results[0].needs_aj
        # ping=0 for unpaid_invoice per seed data
        assert not results[0].autonomous, "needs_aj items must not be autonomous"


# ---------------------------------------------------------------------------
# AC3 — Mixed-sender fixture: same sender, two types → two dispositions
# ---------------------------------------------------------------------------


class TestMixedSenderFixture:
    """One sender produces two message types with different dispositions."""

    def test_same_sender_two_types_two_dispositions(self):
        """A sender emitting both a statement and a receipt gets different dispositions."""
        con = _make_con()
        emails = [
            _email("m1", "noreply@bank.com", "Your monthly statement is ready"),
            _email("m2", "noreply@bank.com", "Transaction receipt — $42.00"),
        ]
        cls_items = [
            _cls("statement", "aware", "file"),  # statement → aware/file
            _cls("receipt", "archive", "file"),  # receipt → archive/file
        ]
        mock_client = _classifier_response(*cls_items)
        with (
            patch.object(skill, "anthropic") as mock_anthropic,
            patch.object(skill, "fetch_calendar_context", return_value=""),
        ):
            mock_anthropic.Anthropic.return_value = mock_client
            results = skill.classify_emails(emails, con)

        tiers = {r.msg_id: r.tier for r in results}
        assert tiers["m1"] == "aware", f"Statement should be aware, got {tiers['m1']}"
        assert tiers["m2"] == "archive", f"Receipt should be archive, got {tiers['m2']}"


# ---------------------------------------------------------------------------
# AC4 — Reversible-only autonomy boundary
# ---------------------------------------------------------------------------


class TestAutonomyBoundary:
    """Autonomous moves may only produce file or quarantine, never trash_direct.
    Acts-tier mail (needs_aj=True) is never autonomous."""

    def _classify(self, cls_items, con):
        emails = [_email(f"m{i}", f"sender{i}@x.com", f"Subj {i}") for i in range(len(cls_items))]
        mock_client = _classifier_response(*cls_items)
        with (
            patch.object(skill, "anthropic") as mock_anthropic,
            patch.object(skill, "fetch_calendar_context", return_value=""),
        ):
            mock_anthropic.Anthropic.return_value = mock_client
            return skill.classify_emails(emails, con)

    def test_high_confidence_file_is_autonomous(self):
        """receipt/archive/file at confidence=0.95 → autonomous=True."""
        con = _make_con()
        results = self._classify([_cls("receipt", "archive", "file", confidence=0.95)], con)
        assert results[0].autonomous, "High-confidence file should be autonomous"

    def test_high_confidence_quarantine_is_autonomous(self):
        """promo/archive/quarantine at confidence=0.95 → autonomous=True."""
        con = _make_con()
        results = self._classify([_cls("promo", "archive", "quarantine", confidence=0.95)], con)
        assert results[0].autonomous, "High-confidence quarantine should be autonomous"

    def test_trash_direct_is_never_autonomous_even_at_max_confidence(self):
        """trash_direct is a non-reversible disposition; must never be autonomous.

        In the new model, trash_direct only fires from the db (daycare_routine seed).
        The classifier returning trash_direct in its JSON is honoured by the type_rules
        table; the autonomy gate then rejects it because disposition is not reversible.
        """
        con = _make_con()
        # daycare_routine has disposition=trash_direct in seeded type_rules
        results = self._classify(
            [_cls("daycare_routine", "archive", "trash_direct", confidence=1.0)], con
        )
        # trash_direct is not in ("file", "quarantine") → reversible=False → autonomous=False
        assert not results[0].autonomous, "trash_direct must never be autonomous"

    def test_low_confidence_is_not_autonomous(self):
        """Below autonomy_threshold (0.85), even reversible moves must not be autonomous."""
        con = _make_con()
        results = self._classify([_cls("receipt", "archive", "file", confidence=0.70)], con)
        assert not results[0].autonomous, "Low-confidence must not be autonomous"

    def test_needs_aj_is_never_autonomous(self):
        """Act-tier mail (needs_aj=True) is never autonomous regardless of confidence."""
        con = _make_con()
        # personal → act/inbox/needs_aj — even if inbox is not a reversible disp,
        # ensure the needs_aj flag alone prevents autonomous
        results = self._classify(
            [_cls("personal", "act", "inbox", needs_aj=True, confidence=0.99)], con
        )
        assert not results[0].autonomous, "needs_aj=True mail must never be autonomous"

    def test_uncertain_is_not_autonomous(self):
        """Classifier-flagged uncertain emails must not be autonomous."""
        con = _make_con()
        results = self._classify(
            [_cls("receipt", "archive", "file", confidence=0.92, uncertain=True)], con
        )
        assert not results[0].autonomous, "uncertain=True must not be autonomous"


# ---------------------------------------------------------------------------
# AC5 — Feedback loop: quarantined item dragged back
# ---------------------------------------------------------------------------


class TestFeedbackLoop:
    """When AJ drags a quarantined item back to inbox, reconcile detects it and
    surfaces a proposed gmail_type_rules correction."""

    def test_reconcile_detects_new_inbox_item_with_quarantine_label(self):
        """_reconcile_corrections must return a non-empty string when a quarantine
        item is found in the inbox (AJ dragged it back)."""
        con = _make_con()
        # Prime seen list as empty (first run)
        seen: list = []
        con.execute(
            "INSERT OR IGNORE INTO gmail_heartbeat_state (key, value) VALUES (?, ?)",
            ("reconcile_corrections_seen", json.dumps(seen)),
        )
        con.commit()

        mock_service = MagicMock()
        # Simulate Gmail returning one message under label:jarvis/quarantine in:inbox
        mock_service.users().messages().list().execute.return_value = {
            "messages": [{"id": "dragged_back_1"}]
        }
        mock_service.users().messages().get().execute.return_value = {
            "id": "dragged_back_1",
            "payload": {
                "headers": [
                    {"name": "From", "value": "promo@example.com"},
                    {"name": "Subject", "value": "Sale ends soon"},
                ]
            },
            "snippet": "Big sale",
        }

        result = skill._reconcile_corrections(mock_service, con)

        assert (
            result
        ), "_reconcile_corrections must return a non-empty string on correction detection"
        # After detection, msg_id should be in seen set so it doesn't fire twice
        seen_after = json.loads(
            con.execute(
                "SELECT value FROM gmail_heartbeat_state WHERE key='reconcile_corrections_seen'"
            ).fetchone()[0]
        )
        assert "dragged_back_1" in seen_after, "Detected msg_id must be added to seen set"

    def test_reconcile_silent_when_already_seen(self):
        """_reconcile_corrections must be silent for msg_ids already in seen set."""
        con = _make_con()
        con.execute(
            "INSERT OR IGNORE INTO gmail_heartbeat_state (key, value) VALUES (?, ?)",
            ("reconcile_corrections_seen", json.dumps(["dragged_back_1"])),
        )
        con.commit()

        mock_service = MagicMock()
        mock_service.users().messages().list().execute.return_value = {
            "messages": [{"id": "dragged_back_1"}]
        }

        result = skill._reconcile_corrections(mock_service, con)
        assert not result, "Must be silent for already-seen msg_ids"
