"""Regression tests for 2026-0045: marketplace seller-reply misclassification.

Covers:
  B1  marketplacereply.* sender → bypass always_inbox → disposition=inbox, tier=act
  B2  _apply_keep_archive_policy sets both action AND disposition → report groups correctly
  B3  save_pending / load_pending round-trips disposition faithfully
  B4  Stage report groups each item under the group that matches its executed disposition
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
_MOD_NAME = "gmail_cleanup_skill_marketplace"
if _MOD_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _SKILL_PATH)
    skill = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = skill
    _spec.loader.exec_module(skill)
else:
    skill = sys.modules[_MOD_NAME]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_con() -> sqlite3.Connection:
    """In-memory DB with all gmail tables + seeded decision layer."""
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
        CREATE TABLE IF NOT EXISTS gmail_pending_actions (
            msg_id TEXT PRIMARY KEY,
            sender_email TEXT NOT NULL,
            sender_display TEXT NOT NULL,
            subject TEXT NOT NULL,
            action TEXT NOT NULL,
            tag TEXT NOT NULL DEFAULT 'none',
            reason TEXT,
            staged_at TEXT DEFAULT (datetime('now')),
            label_ids_json TEXT NOT NULL DEFAULT '[]',
            disposition TEXT NOT NULL DEFAULT 'file',
            tier TEXT NOT NULL DEFAULT 'archive',
            email_type TEXT NOT NULL DEFAULT '',
            needs_aj INTEGER NOT NULL DEFAULT 0,
            calendar_hint INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 1.0,
            autonomous INTEGER NOT NULL DEFAULT 0,
            uncertain INTEGER NOT NULL DEFAULT 0,
            watch_label TEXT NOT NULL DEFAULT ''
        );
    """)
    skill._seed_decision_layer(con)
    for name, defn in [
        ("receipts", "Purchase confirmations and order tracking"),
        ("none", "Transient — pending tier-2 resolution"),
        ("other", "Catch-all"),
        ("personal", "Personal correspondence"),
    ]:
        con.execute(
            "INSERT OR IGNORE INTO gmail_tags (name, definition, created_at) VALUES (?, ?, '2026-01-01')",
            (name, defn),
        )
    con.commit()
    return con


def _email(msg_id: str, sender: str, subject: str, snippet: str = "") -> dict:
    return {
        "id": msg_id,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "parts": [],
        },
        "snippet": snippet or subject,
        "labelIds": [],
    }


def _make_summary(
    msg_id: str = "m1",
    sender_email: str = "test@example.com",
    action: str = "keep",
    tag: str = "none",
    disposition: str = "inbox",
    tier: str = "act",
) -> skill.EmailSummary:
    return skill.EmailSummary(
        msg_id=msg_id,
        sender=sender_email,
        sender_email=sender_email,
        subject="Test subject",
        action=action,
        reason="test",
        tag=tag,
        disposition=disposition,
        tier=tier,
    )


# ---------------------------------------------------------------------------
# B1 — marketplacereply.* bypass → always_inbox
# ---------------------------------------------------------------------------


class TestMarketplaceReplyBypass:
    """Sender matching marketplacereply.* must hit the always_inbox bypass,
    never reaching Haiku, regardless of subject content."""

    def _run_classify(self, emails, con):
        mock_client = MagicMock()
        with (
            patch.object(skill, "anthropic") as mock_anthropic,
            patch.object(skill, "fetch_calendar_context", return_value=""),
        ):
            mock_anthropic.Anthropic.return_value = mock_client
            return skill.classify_emails(emails, con)

    def test_seller_reply_bestbuy_disposition_inbox(self):
        """bestbuympgop54wnij2@marketplacereply.bestbuy.ca → disposition=inbox."""
        con = _make_con()
        emails = [
            _email(
                "m1",
                "bestbuympgop54wnij2@marketplacereply.bestbuy.ca",
                "Seller response to your message",
            )
        ]
        results = self._run_classify(emails, con)

        assert len(results) == 1
        r = results[0]
        assert r.disposition == "inbox", f"Expected inbox, got {r.disposition}"
        assert r.tier == "act", f"Expected act, got {r.tier}"

    def test_seller_reply_does_not_reach_haiku(self):
        """Bypass fires before Haiku — Anthropic client must not be called."""
        con = _make_con()
        emails = [
            _email(
                "m2",
                "seller@marketplacereply.amazon.ca",
                "Seller response to your message",
            )
        ]
        mock_client = MagicMock()
        with (
            patch.object(skill, "anthropic") as mock_anthropic,
            patch.object(skill, "fetch_calendar_context", return_value=""),
        ):
            mock_anthropic.Anthropic.return_value = mock_client
            skill.classify_emails(emails, con)
            mock_client.messages.create.assert_not_called()

    def test_non_marketplace_reply_subdomain_not_bypassed(self):
        """A regular bestbuy.ca sender must NOT be bypassed — only marketplacereply.*."""
        con = _make_con()
        emails = [
            _email(
                "m3",
                "noreply@bestbuy.ca",
                "Your order has shipped",
            )
        ]
        mock_resp = MagicMock()
        mock_resp.content = [
            MagicMock(
                text=json.dumps(
                    [
                        {
                            "type": "receipt",
                            "tier": "archive",
                            "disposition": "file",
                            "needs_aj": False,
                            "confidence": 0.95,
                            "calendar_hint": False,
                            "uncertain": False,
                            "reason": "order shipped",
                            "tag": "receipts",
                        }
                    ]
                )
            )
        ]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        with (
            patch.object(skill, "anthropic") as mock_anthropic,
            patch.object(skill, "fetch_calendar_context", return_value=""),
        ):
            mock_anthropic.Anthropic.return_value = mock_client
            results = skill.classify_emails(emails, con)

        assert len(results) == 1
        assert (
            results[0].disposition != "inbox"
        ), "Regular bestbuy.ca sender should not be bypassed to inbox"

    def test_marketplacereply_seed_present_in_decision_layer(self):
        """gmail_senders must contain marketplacereply. after seed."""
        con = _make_con()
        row = con.execute(
            "SELECT bypass FROM gmail_senders WHERE sender_pattern = 'marketplacereply.'"
        ).fetchone()
        assert row is not None, "marketplacereply. seed missing from gmail_senders"
        assert row[0] == "always_inbox", f"Expected always_inbox bypass, got {row[0]}"


# ---------------------------------------------------------------------------
# B2 — _apply_keep_archive_policy sets disposition=file (not just action)
# ---------------------------------------------------------------------------


class TestKeepArchivePolicyDisposition:
    """After _apply_keep_archive_policy fires, s.disposition must be 'file'
    so the staging report groups the item under FILE, not INBOX."""

    def test_receipts_tag_keep_sets_disposition_file(self):
        """tag=receipts + action=keep → policy sets disposition=file."""
        s = _make_summary(action="keep", tag="receipts", disposition="inbox", tier="archive")
        skill._apply_keep_archive_policy([s])
        assert s.action == "archive", f"Expected archive action, got {s.action}"
        assert s.disposition == "file", f"Expected file disposition, got {s.disposition}"

    def test_bills_tag_keep_sets_disposition_file(self):
        """tag=bills + action=keep → policy sets disposition=file."""
        s = _make_summary(action="keep", tag="bills", disposition="inbox", tier="archive")
        skill._apply_keep_archive_policy([s])
        assert s.disposition == "file", f"Expected file, got {s.disposition}"

    def test_personal_tag_not_affected(self):
        """tag=personal is not in KEEP_ARCHIVE_TAGS — disposition stays inbox."""
        s = _make_summary(action="keep", tag="personal", disposition="inbox", tier="act")
        skill._apply_keep_archive_policy([s])
        assert s.disposition == "inbox", f"Personal mail should stay inbox, got {s.disposition}"

    def test_security_subject_prevents_archive(self):
        """Security keyword in subject prevents keep-archive policy."""
        s = _make_summary(action="keep", tag="receipts", disposition="inbox")
        s.subject = "Unauthorized login attempt on your account"
        skill._apply_keep_archive_policy([s])
        assert s.disposition == "inbox", "Security keyword should block keep-archive"


# ---------------------------------------------------------------------------
# B3 — save_pending / load_pending round-trips disposition
# ---------------------------------------------------------------------------


class TestPendingDispositionRoundTrip:
    """disposition must survive save→load so execute_actions sees the right value."""

    def test_inbox_disposition_survives_round_trip(self):
        con = _make_con()
        s = _make_summary(msg_id="rt1", action="keep", disposition="inbox")
        skill.save_pending(con, [s])
        loaded = skill.load_pending(con)
        assert len(loaded) == 1
        assert (
            loaded[0].disposition == "inbox"
        ), f"inbox disposition lost on reload, got {loaded[0].disposition}"

    def test_file_disposition_survives_round_trip(self):
        con = _make_con()
        s = _make_summary(msg_id="rt2", action="archive", disposition="file")
        skill.save_pending(con, [s])
        loaded = skill.load_pending(con)
        assert loaded[0].disposition == "file"

    def test_quarantine_disposition_survives_round_trip(self):
        con = _make_con()
        s = _make_summary(msg_id="rt3", action="archive", disposition="quarantine")
        skill.save_pending(con, [s])
        loaded = skill.load_pending(con)
        assert loaded[0].disposition == "quarantine"

    def test_trash_direct_disposition_survives_round_trip(self):
        con = _make_con()
        s = _make_summary(msg_id="rt4", action="trash", disposition="trash_direct")
        skill.save_pending(con, [s])
        loaded = skill.load_pending(con)
        assert loaded[0].disposition == "trash_direct"


# ---------------------------------------------------------------------------
# B4 — staging report grouping matches executed disposition
# ---------------------------------------------------------------------------


class TestStagingReportGroupMatchesDisposition:
    """The group each email appears under in build_staging_report must match
    the disposition that execute_actions will act on."""

    def test_receipt_keep_archive_appears_in_file_group(self):
        """A receipt tagged email after keep-archive policy must appear under FILE, not INBOX."""
        s = _make_summary(action="keep", tag="receipts", disposition="inbox")
        skill._apply_keep_archive_policy([s])
        report = skill.build_staging_report([s], dry_run=True)
        assert "FILE" in report, "Report must contain a FILE group"
        # The sender email must appear under FILE, not INBOX
        lines = report.splitlines()
        in_file_section = False
        found_in_file = False
        found_in_inbox = False
        for line in lines:
            if "FILE" in line and "**" in line:
                in_file_section = True
            elif "**" in line and "FILE" not in line:
                in_file_section = False
            if s.sender_email in line:
                if in_file_section:
                    found_in_file = True
                else:
                    found_in_inbox = True
        assert found_in_file, "Receipt email should appear in FILE section of report"
        assert not found_in_inbox, "Receipt email must NOT appear in INBOX section of report"

    def test_inbox_disposition_appears_in_inbox_group(self):
        """Correspondence (disposition=inbox) must appear under INBOX (act)."""
        s = _make_summary(action="keep", tag="personal", disposition="inbox", tier="act")
        report = skill.build_staging_report([s], dry_run=True)
        lines = report.splitlines()
        in_inbox_section = False
        found_in_inbox = False
        for line in lines:
            if "INBOX" in line and "**" in line:
                in_inbox_section = True
            elif "**" in line and "INBOX" not in line:
                in_inbox_section = False
            if s.sender_email in line and in_inbox_section:
                found_in_inbox = True
        assert found_in_inbox, "Personal email should appear in INBOX section"

    def test_report_group_matches_execute_disposition(self):
        """For a mixed batch: each item's report group must equal its disposition."""
        summaries = [
            _make_summary("e1", "person@example.com", "keep", "personal", "inbox", "act"),
            _make_summary("e2", "shop@store.com", "archive", "receipts", "file", "archive"),
            _make_summary("e3", "spam@promo.com", "archive", "none", "quarantine", "archive"),
        ]
        report = skill.build_staging_report(summaries, dry_run=True)
        disp_to_group = {
            "inbox": "INBOX",
            "file": "FILE",
            "quarantine": "QUARANTINE",
            "trash_direct": "TRASH",
        }
        lines = report.splitlines()
        current_group = None
        sender_to_group: dict[str, str] = {}
        for line in lines:
            for group_key in ("INBOX", "FILE", "QUARANTINE", "TRASH"):
                if f"**{group_key}" in line or f"**{group_key}" in line.upper():
                    current_group = group_key
                    break
            for s in summaries:
                if s.sender_email in line and current_group:
                    sender_to_group[s.sender_email] = current_group

        for s in summaries:
            expected_group = disp_to_group[s.disposition]
            actual_group = sender_to_group.get(s.sender_email)
            assert (
                actual_group == expected_group
            ), f"{s.sender_email}: expected group {expected_group}, found in {actual_group}"
