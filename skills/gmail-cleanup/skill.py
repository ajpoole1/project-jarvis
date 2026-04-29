"""Gmail cleanup skill — classifies and stages inbox actions for user approval."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv(Path(__file__).parents[2] / ".env")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
CONFIG_DIR = Path(os.environ.get("JARVIS_CONFIG_DIR", "/config/personal"))
DB_PATH = DATA_DIR / "jarvis.db"
CREDENTIALS_PATH = CONFIG_DIR / "gmail_credentials.json"
TOKEN_PATH = CONFIG_DIR / "gmail_token.json"

DEFAULT_BATCH_SIZE = int(os.environ.get("GMAIL_BATCH_SIZE", "50"))
DRY_RUN = os.environ.get("GMAIL_DRY_RUN", "true").lower() == "true"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

ACTIONS = ("archive", "trash", "unsubscribe", "keep")
TAGS = ("receipts", "bills", "job-search", "health", "family", "projects", "none")

RULES_PATH = CONFIG_DIR / "gmail_rules.json"
RULES_EXAMPLE_PATH = Path(__file__).parents[2] / "config" / "examples" / "gmail_rules.json"


def _load_rules() -> dict:
    path = RULES_PATH if RULES_PATH.exists() else RULES_EXAMPLE_PATH
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


_RULES = _load_rules()

NEVER_CACHE_SENDERS: set[str] = {s.lower() for s in _RULES.get("never_cache_senders", [])}

CALENDAR_SUBJECT_KEYWORDS = frozenset(
    {
        "appointment",
        "reminder",
        "booking",
        "reservation",
        "confirmation",
        "your visit",
        "scheduled",
        "upcoming",
        "check-up",
        "checkup",
        "follow-up",
        "follow up",
    }
)

PRIORITY_TAGS = frozenset({"family"})
SECURITY_KEYWORDS = frozenset(
    {
        "security alert",
        "unauthorized",
        "suspicious",
        "breach",
        "password reset",
        "verify your",
        "login attempt",
        "new sign-in",
        "two-factor",
        "account locked",
    }
)
FINANCIAL_KEYWORDS = frozenset(
    {
        "low balance",
        "fraud alert",
        "unusual activity",
        "cra ",
        "revenue canada",
        "payment declined",
        "refund issued",
        "tax notice",
    }
)
AUTOMATED_PREFIXES = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "notifications",
        "updates",
        "newsletter",
        "mailer",
        "info",
        "support",
        "help",
        "admin",
        "system",
        "automated",
        "auto",
        "bounce",
        "postmaster",
    }
)


def _build_priority_rules() -> str:
    """Build the PRIORITY RULES prompt section from gmail_rules.json."""
    lines = ["PRIORITY RULES (apply in order, first match wins):"]
    idx = 1

    for sender in _RULES.get("priority_senders", []):
        email = sender["email"]
        for rule in sender.get("rules", []):
            action = rule["action"]
            reason = rule.get("reason", "")
            tag = f", tag={rule['tag']}" if "tag" in rule else ""
            subject_cond = rule.get("subject_contains")
            if subject_cond:
                lines.append(
                    f'{idx}. {email} AND subject contains "{subject_cond}": '
                    f"ALWAYS {action} — {reason}. Applies even if subject mentions family names."
                )
            else:
                lines.append(f"{idx}. {email} (all other subjects): {action}{tag} — {reason}")
            idx += 1

    names = _RULES.get("always_keep_names", [])
    if names:
        quoted = ", ".join(f'"{n}"' for n in names)
        lines.append(f"{idx}. Any email referencing {quoted}: always keep")

    return "\n".join(lines) if idx > 1 else ""


LABEL_PREFIX = "jarvis"


@dataclass
class EmailSummary:
    msg_id: str
    sender: str
    sender_email: str
    subject: str
    action: str
    reason: str
    tag: str = "none"
    calendar_hint: bool = field(default=False)


def get_gmail_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gmail_sender_rules (
            sender_email TEXT PRIMARY KEY,
            action       TEXT NOT NULL,
            confirmed    INTEGER NOT NULL DEFAULT 0,
            last_applied TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gmail_pending_actions (
            msg_id          TEXT PRIMARY KEY,
            sender_email    TEXT NOT NULL,
            sender_display  TEXT NOT NULL,
            subject         TEXT NOT NULL,
            action          TEXT NOT NULL,
            tag             TEXT NOT NULL DEFAULT 'none',
            reason          TEXT,
            staged_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gmail_heartbeat_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    con.commit()
    return con


def get_heartbeat_state(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM gmail_heartbeat_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_heartbeat_state(con: sqlite3.Connection, key: str, value: str):
    con.execute(
        "INSERT OR REPLACE INTO gmail_heartbeat_state (key, value) VALUES (?, ?)",
        (key, value),
    )
    con.commit()


def save_pending(con: sqlite3.Connection, summaries: list[EmailSummary]):
    con.execute("DELETE FROM gmail_pending_actions")
    for s in summaries:
        con.execute(
            """INSERT OR REPLACE INTO gmail_pending_actions
               (msg_id, sender_email, sender_display, subject, action, tag, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (s.msg_id, s.sender_email, s.sender, s.subject, s.action, s.tag, s.reason),
        )
    con.commit()


def load_pending(con: sqlite3.Connection) -> list[EmailSummary]:
    rows = con.execute(
        "SELECT msg_id, sender_email, sender_display, subject, action, tag, reason FROM gmail_pending_actions"
    ).fetchall()
    return [
        EmailSummary(
            msg_id=r[0],
            sender_email=r[1],
            sender=r[2],
            subject=r[3],
            action=r[4],
            tag=r[5],
            reason=r[6] or "",
        )
        for r in rows
    ]


def clear_pending(con: sqlite3.Connection):
    con.execute("DELETE FROM gmail_pending_actions")
    con.commit()


def adjust_pending(con: sqlite3.Connection, sender_email: str, action: str) -> str:
    if action not in ACTIONS:
        return f"Unknown action '{action}'. Choose from: {', '.join(ACTIONS)}"
    cursor = con.execute(
        "UPDATE gmail_pending_actions SET action = ? WHERE sender_email = ?",
        (action, sender_email.lower()),
    )
    con.commit()
    if cursor.rowcount == 0:
        return f"No pending email from {sender_email}."
    return f"Updated: {sender_email} → {action}"


def get_cached_action(con: sqlite3.Connection, sender_email: str) -> str | None:
    row = con.execute(
        "SELECT action FROM gmail_sender_rules WHERE sender_email = ? AND confirmed = 1",
        (sender_email,),
    ).fetchone()
    return row[0] if row else None


def cache_rule(con: sqlite3.Connection, sender_email: str, action: str, confirmed: bool = False):
    con.execute(
        """
        INSERT INTO gmail_sender_rules (sender_email, action, confirmed, last_applied)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(sender_email) DO UPDATE SET
            action = excluded.action,
            confirmed = excluded.confirmed,
            last_applied = excluded.last_applied
        """,
        (sender_email, action, int(confirmed)),
    )
    con.commit()


def parse_sender(raw: str) -> tuple[str, str]:
    """Return (display_name, email) from a raw From header."""
    if "<" in raw:
        name = raw.split("<")[0].strip().strip('"')
        email = raw.split("<")[1].rstrip(">").strip()
    else:
        name = raw.strip()
        email = raw.strip()
    return name, email.lower()


def _looks_automated(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(local == p or local.startswith(p) for p in AUTOMATED_PREFIXES)


def _is_priority(summary: EmailSummary) -> bool:
    if summary.tag in PRIORITY_TAGS:
        return True
    if summary.calendar_hint:
        return True
    subject_lower = summary.subject.lower()
    if any(kw in subject_lower for kw in SECURITY_KEYWORDS):
        return True
    if any(kw in subject_lower for kw in FINANCIAL_KEYWORDS):
        return True
    if summary.action == "keep" and not _looks_automated(summary.sender_email):
        return True
    return False


def fetch_inbox_messages(service, batch_size: int) -> list[dict]:
    msg_ids = []
    page_token = None
    while len(msg_ids) < batch_size:
        fetch = min(500, batch_size - len(msg_ids))
        kwargs = {"userId": "me", "labelIds": ["INBOX"], "maxResults": fetch}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        msg_ids += [m["id"] for m in result.get("messages", [])]
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    messages = []
    for msg_id in msg_ids:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            )
            .execute()
        )
        messages.append(msg)
    return messages


def fetch_new_messages(service, since_epoch: int | None, batch_size: int) -> list[dict]:
    """Fetch inbox messages newer than since_epoch (Unix seconds). No filter if None."""
    query = "in:inbox"
    if since_epoch:
        query += f" after:{since_epoch}"
    msg_ids = []
    page_token = None
    while len(msg_ids) < batch_size:
        fetch = min(500, batch_size - len(msg_ids))
        kwargs = {"userId": "me", "q": query, "maxResults": fetch}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        msg_ids += [m["id"] for m in result.get("messages", [])]
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    messages = []
    for msg_id in msg_ids:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            )
            .execute()
        )
        messages.append(msg)
    return messages


def fetch_calendar_context(days: int = 30) -> str:
    """Return upcoming calendar events as a prompt-ready string. Empty string if unavailable."""
    skill_path = Path(__file__).parents[1] / "calendar" / "skill.py"
    python_path = Path(__file__).parents[1] / "calendar" / ".venv" / "bin" / "python"
    if not skill_path.exists() or not python_path.exists():
        return ""
    try:
        result = subprocess.run(
            [str(python_path), str(skill_path), "upcoming", str(days)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        events = json.loads(result.stdout)
        if not events:
            return ""
        lines = [f"Upcoming calendar events (next {days} days):"]
        for e in events:
            start = e.get("start", "")[:10]
            summary = e.get("summary", "")
            lines.append(f"- {start}: {summary}")
        return "\n".join(lines)
    except Exception:
        return ""


def classify_emails(emails: list[dict], con: sqlite3.Connection) -> list[EmailSummary]:
    client = anthropic.Anthropic()
    results = []

    uncached = []
    for msg in emails:
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")
        name, email = parse_sender(raw_from)

        snippet = msg.get("snippet", "")
        cached = None if email in NEVER_CACHE_SENDERS else get_cached_action(con, email)
        if cached:
            results.append(
                EmailSummary(
                    msg_id=msg["id"],
                    sender=name,
                    sender_email=email,
                    subject=subject,
                    action=cached,
                    reason="cached rule",
                )
            )
        else:
            uncached.append((msg["id"], name, email, subject, snippet))

    CLASSIFY_CHUNK = 50
    calendar_context = fetch_calendar_context()
    priority_rules = _build_priority_rules()

    def _build_prompt(batch_input: str) -> str:
        parts = ["Classify each email as one of: archive, trash, unsubscribe, keep.\n"]
        if calendar_context:
            parts.append(calendar_context + "\n")
        parts.append(
            "Rules:\n"
            "- keep: personal correspondence from real people, financial alerts (low balance, fraud, CRA/tax), health/medical, travel bookings, anything related to the user's family\n"
            "- archive: invoices, receipts, and billing statements from non-Amazon vendors (banks, insurance, software, professional services), job applications, account statements\n"
            "- trash: ALL Amazon order confirmations and shipping notifications, ALL Shopify merchant shipping/delivery emails, any order or tracking email from a retail store or marketplace\n"
            "- unsubscribe: marketing/promotional email, retail sale announcements, newsletters the user did not explicitly request\n"
            '- trash: spam, irrelevant bulk mail, duplicate notifications, automated alerts with no action required, ANY email from a retailer or vendor that does not contain a specific order number, tracking number, or account-specific transaction detail — generic "sale", "new arrivals", "don\'t miss out" emails from stores are always trash even if the store is known\n'
        )
        if priority_rules:
            parts.append(f"\n{priority_rules}\n")
        parts.append(
            "\nAPPOINTMENT RULE: If an email is a confirmation, reminder, or scheduling notice for an appointment already listed in the calendar above, archive it — it is already saved. "
            "If it is appointment-related but NOT on the calendar, keep it AND set calendar_hint: true.\n"
            "\nSet calendar_hint: true whenever the email contains scheduling information (date, time, location) for an appointment, booking, or event not already on the calendar — "
            "use the Preview field if the subject alone is ambiguous.\n"
            "\nAlso assign a tag from: receipts, bills, job-search, health, family, projects, none\n"
            "\nRespond with a JSON array, one object per email, in the same order:\n"
            '[{"action": "keep", "tag": "health", "reason": "brief reason", "calendar_hint": true}, ...]\n'
            f"\nEmails:\n{batch_input}"
        )
        return "\n".join(parts)

    for chunk_start in range(0, len(uncached), CLASSIFY_CHUNK):
        chunk = uncached[chunk_start : chunk_start + CLASSIFY_CHUNK]
        batch_input = "\n".join(
            f'{i+1}. From: "{name}" <{email}> | Subject: {subject}'
            + (f"\n   Preview: {snippet[:150]}" if snippet else "")
            for i, (_, name, email, subject, snippet) in enumerate(chunk)
        )
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": _build_prompt(batch_input)}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            classifications = json.loads(raw)
        except json.JSONDecodeError:
            classifications = [
                {"action": "keep", "reason": "parse error — defaulting to keep"} for _ in chunk
            ]

        for (msg_id, name, email, subject, _snippet), cls in zip(
            chunk, classifications, strict=False
        ):
            action = cls.get("action", "keep")
            if action not in ACTIONS:
                action = "keep"
            tag = cls.get("tag", "none")
            if tag not in TAGS:
                tag = "none"
            results.append(
                EmailSummary(
                    msg_id=msg_id,
                    sender=name,
                    sender_email=email,
                    subject=subject,
                    action=action,
                    reason=cls.get("reason", ""),
                    tag=tag,
                    calendar_hint=bool(cls.get("calendar_hint", False)),
                )
            )
            if email not in NEVER_CACHE_SENDERS:
                cache_rule(con, email, action, confirmed=False)

    return results


def build_staging_report(summaries: list[EmailSummary], dry_run: bool) -> str:
    grouped: dict[str, list[EmailSummary]] = {a: [] for a in ACTIONS}
    for s in summaries:
        grouped[s.action].append(s)

    total = sum(len(v) for v in grouped.values())
    lines = [
        f"**Gmail cleanup — {'DRY RUN ' if dry_run else ''}staged actions** ({total} emails fetched)\n"
    ]
    for action in ("trash", "unsubscribe", "archive", "keep"):
        items = grouped[action]
        if not items:
            continue
        lines.append(f"**{action.upper()} ({len(items)})**")
        for item in items[:10]:
            cal = " [needs calendar]" if item.calendar_hint else ""
            lines.append(f"  • {item.sender_email} — {item.subject[:60]}{cal}")
        if len(items) > 10:
            lines.append(f"  _…and {len(items) - 10} more_")
    lines.append(
        f"\n{'⚠️ Dry run — no changes made.' if dry_run else 'Reply **confirm** to execute, or **cancel** to abort.'}"
    )
    return "\n".join(lines)


def get_or_create_labels(service) -> dict[str, str]:
    """Return a map of tag name → Gmail label ID, creating labels that don't exist."""
    existing = {
        lbl["name"]: lbl["id"]
        for lbl in service.users().labels().list(userId="me").execute().get("labels", [])
    }
    label_map = {}
    for tag in TAGS:
        if tag == "none":
            continue
        name = f"{LABEL_PREFIX}/{tag}"
        if name in existing:
            label_map[tag] = existing[name]
        else:
            created = (
                service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            label_map[tag] = created["id"]
    return label_map


def execute_actions(
    service,
    summaries: list[EmailSummary],
    con: sqlite3.Connection,
    label_map: dict[str, str] | None = None,
):
    for s in summaries:
        add_labels = []
        if label_map and s.tag and s.tag != "none" and s.tag in label_map:
            add_labels = [label_map[s.tag]]
        if s.action == "archive":
            service.users().messages().modify(
                userId="me",
                id=s.msg_id,
                body={"removeLabelIds": ["INBOX"], "addLabelIds": add_labels},
            ).execute()
        elif s.action in ("trash", "unsubscribe"):
            service.users().messages().trash(userId="me", id=s.msg_id).execute()
        elif s.action == "keep" and add_labels:
            service.users().messages().modify(
                userId="me",
                id=s.msg_id,
                body={"addLabelIds": add_labels},
            ).execute()
        cache_rule(con, s.sender_email, s.action, confirmed=True)


def review_pending(con: sqlite3.Connection) -> str:
    rows = con.execute(
        "SELECT sender_email, action FROM gmail_sender_rules WHERE confirmed = 0 ORDER BY action, sender_email"
    ).fetchall()
    if not rows:
        return "No pending rules to review."

    grouped: dict[str, list[str]] = {a: [] for a in ACTIONS}
    for email, action in rows:
        grouped[action].append(email)

    lines = [f"**Pending unconfirmed rules ({len(rows)} senders)**\n"]
    for action in ("trash", "unsubscribe", "archive", "keep"):
        senders = grouped[action]
        if not senders:
            continue
        lines.append(f"**{action.upper()} ({len(senders)})**")
        for sender in senders:
            lines.append(f"  • {sender}")
    lines.append("\nRun `confirm_action <action>` or `confirm_all` to lock these in.")
    return "\n".join(lines)


def confirm_action(con: sqlite3.Connection, action: str) -> str:
    if action not in ACTIONS:
        return f"Unknown action '{action}'. Choose from: {', '.join(ACTIONS)}"
    cursor = con.execute(
        "UPDATE gmail_sender_rules SET confirmed = 1 WHERE action = ? AND confirmed = 0",
        (action,),
    )
    con.commit()
    return f"Confirmed {cursor.rowcount} sender rules as '{action}'."


def confirm_all(con: sqlite3.Connection) -> str:
    cursor = con.execute("UPDATE gmail_sender_rules SET confirmed = 1 WHERE confirmed = 0")
    con.commit()
    return f"Confirmed {cursor.rowcount} pending sender rules."


def override_rule(con: sqlite3.Connection, sender_email: str, action: str) -> str:
    if action not in ACTIONS:
        return f"Unknown action '{action}'. Choose from: {', '.join(ACTIONS)}"
    cache_rule(con, sender_email.lower(), action, confirmed=True)
    return f"Rule set: {sender_email} → {action} (confirmed)."


def stage(batch_size: int = DEFAULT_BATCH_SIZE) -> str:
    """Classify inbox and save pending actions to SQLite. Nothing is executed."""
    service = get_gmail_service()
    con = init_db()
    messages = fetch_inbox_messages(service, batch_size)
    summaries = classify_emails(messages, con)
    save_pending(con, summaries)
    report = build_staging_report(summaries, dry_run=True)
    actionable = [s for s in summaries if s.action != "keep"]
    if not actionable:
        return report
    return (
        report
        + "\n\nReply **execute** to apply, **cancel** to abort, or **adjust <sender> <action>** to change individual items."
    )


def cmd_execute() -> str:
    """Apply all pending staged actions."""
    service = get_gmail_service()
    con = init_db()
    summaries = load_pending(con)
    if not summaries:
        return "No pending actions. Run the gmail cleanup first to stage actions."
    label_map = get_or_create_labels(service)
    non_keep = [s for s in summaries if s.action != "keep"]
    keep = [s for s in summaries if s.action == "keep"]
    execute_actions(service, non_keep + keep, con, label_map)
    clear_pending(con)
    actioned = len(non_keep)
    return f"Done. {actioned} email{'s' if actioned != 1 else ''} actioned, {len(keep)} kept."


def cmd_cancel() -> str:
    """Discard pending staged actions without executing."""
    con = init_db()
    pending = load_pending(con)
    if not pending:
        return "No pending actions to cancel."
    clear_pending(con)
    return f"Cancelled. {len(pending)} staged actions discarded — nothing was changed."


def cmd_pending() -> str:
    """Show current staged actions waiting for approval."""
    con = init_db()
    summaries = load_pending(con)
    if not summaries:
        return "No pending actions staged."
    return build_staging_report(summaries, dry_run=True)


def cmd_adjust(sender_email: str, action: str) -> str:
    """Change the staged action for a specific sender before executing."""
    con = init_db()
    return adjust_pending(con, sender_email, action)


# keep `run` as an alias so heartbeat/drain callers still work
def run(batch_size: int = DEFAULT_BATCH_SIZE, dry_run: bool = DRY_RUN) -> str:
    if dry_run:
        return stage(batch_size)
    result = stage(batch_size)
    con = init_db()
    summaries = load_pending(con)
    if any(s.action != "keep" for s in summaries):
        result += "\n" + cmd_execute()
    return result


def _decode_body_part(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        text = _decode_body_part(part)
        if text:
            return text
    return ""


def fetch_body(service, msg_id: str, max_chars: int = 1500) -> str:
    """Return the plain-text body of a single email (falls back to snippet)."""
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    body = _decode_body_part(msg.get("payload", {}))
    return (body or msg.get("snippet", ""))[:max_chars].strip()


def cmd_body(msg_id: str) -> str:
    """Fetch and return the plain-text body of an email by message ID."""
    if not msg_id:
        return "Usage: body <msg_id>"
    service = get_gmail_service()
    return fetch_body(service, msg_id) or "(no text content)"


def cmd_heartbeat(batch_size: int = 50) -> str:
    """Incremental inbox check. Pings immediately for priority mail; queues the rest for digest."""
    service = get_gmail_service()
    con = init_db()

    last_checked_str = get_heartbeat_state(con, "last_checked")
    if not last_checked_str:
        set_heartbeat_state(con, "last_checked", datetime.utcnow().isoformat())
        return "SILENT"

    since_epoch = int(datetime.fromisoformat(last_checked_str).timestamp())
    now = datetime.utcnow()
    messages = fetch_new_messages(service, since_epoch, batch_size)
    set_heartbeat_state(con, "last_checked", now.isoformat())

    if not messages:
        return "SILENT"

    summaries = classify_emails(messages, con)
    priority = [s for s in summaries if _is_priority(s)]
    digest_items = [s for s in summaries if not _is_priority(s) and s.action != "keep"]

    output_parts = []

    if priority:
        lines = ["IMMEDIATE:"]
        for s in priority:
            cal = " [needs calendar]" if s.calendar_hint else ""
            lines.append(f"  • {s.sender} — {s.subject[:70]}{cal} [{s.tag}]")
        lines.append("\nReply **gmail stage** to run full cleanup.")
        output_parts.append("\n".join(lines))

    if digest_items:
        existing_json = get_heartbeat_state(con, "digest_queue") or "[]"
        queue = json.loads(existing_json)
        for s in digest_items:
            queue.append(
                {"sender": s.sender, "subject": s.subject, "action": s.action, "tag": s.tag}
            )
        set_heartbeat_state(con, "digest_queue", json.dumps(queue))
        output_parts.append(
            f"DIGEST ADDED: {len(digest_items)} emails queued ({len(queue)} total pending)."
        )

    return "\n\n".join(output_parts) if output_parts else "SILENT"


def cmd_digest() -> str:
    """Post the queued digest of non-priority actionable emails and clear the queue."""
    con = init_db()
    queue_json = get_heartbeat_state(con, "digest_queue") or "[]"
    queue = json.loads(queue_json)

    if not queue:
        return "DIGEST EMPTY"

    grouped: dict[str, list] = {a: [] for a in ACTIONS}
    for item in queue:
        grouped[item["action"]].append(item)

    total = len(queue)
    lines = [f"**Gmail digest** ({total} emails to clean up)\n"]
    for action in ("trash", "unsubscribe", "archive"):
        items = grouped[action]
        if not items:
            continue
        lines.append(f"**{action.upper()} ({len(items)})**")
        for item in items[:8]:
            lines.append(f"  • {item['subject'][:60]}")
        if len(items) > 8:
            lines.append(f"  _…and {len(items) - 8} more_")
    lines.append("\nReply **gmail stage** to review and execute cleanup.")
    set_heartbeat_state(con, "digest_queue", "[]")
    return "\n".join(lines)


def purge_archive(batch_size: int = 500):
    """Trash archived emails from senders confirmed as trash/unsubscribe in SQLite."""
    service = get_gmail_service()
    con = init_db()
    rows = con.execute(
        "SELECT sender_email FROM gmail_sender_rules WHERE action IN ('trash', 'unsubscribe') AND confirmed = 1"
    ).fetchall()
    senders = [r[0] for r in rows]
    if not senders:
        print("No confirmed trash/unsubscribe senders in cache.")
        return
    print(f"Purging archive for {len(senders)} known senders...")
    total = 0
    for sender in senders:
        page_token = None
        while True:
            kwargs = {
                "userId": "me",
                "q": f"from:{sender} -in:inbox -in:trash -in:spam",
                "maxResults": batch_size,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.users().messages().list(**kwargs).execute()
            messages = result.get("messages", [])
            if not messages:
                break
            for msg in messages:
                service.users().messages().trash(userId="me", id=msg["id"]).execute()
            total += len(messages)
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    print(f"Purge complete. {total} archived emails trashed.")


def drain_categories(batch_size: int = DEFAULT_BATCH_SIZE):
    """Drain Updates and Purchases tabs through the classifier."""
    service = get_gmail_service()
    con = init_db()
    for label in ("CATEGORY_UPDATES", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"):
        total = 0
        print(f"\nDraining {label}...")
        while True:
            result = (
                service.users()
                .messages()
                .list(userId="me", labelIds=[label], maxResults=min(batch_size, 500))
                .execute()
            )
            msg_stubs = result.get("messages", [])
            if not msg_stubs:
                print(f"  {label} clear. {total} actioned.")
                break
            messages = [
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=m["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
                for m in msg_stubs
            ]
            summaries = classify_emails(messages, con)
            non_keep = [s for s in summaries if s.action != "keep"]
            if not non_keep and total > 0:
                print(f"  {label} done. {total} actioned, {len(summaries)} kept.")
                break
            execute_actions(service, non_keep, con)
            # strip the category label from all processed messages so they don't get refetched
            for msg in messages:
                service.users().messages().modify(
                    userId="me",
                    id=msg["id"],
                    body={"removeLabelIds": [label]},
                ).execute()
            total += len(non_keep)
            print(f"  {label}: {total} actioned so far ({len(summaries) - len(non_keep)} kept)...")


def drain(batch_size: int = DEFAULT_BATCH_SIZE):
    """Repeatedly process inbox until no actionable emails remain."""
    service = get_gmail_service()
    con = init_db()
    label_map = get_or_create_labels(service)
    total_actioned = 0
    run_count = 0
    while True:
        run_count += 1
        messages = fetch_inbox_messages(service, batch_size)
        if not messages:
            print(f"Inbox empty. Done in {run_count - 1} passes, {total_actioned} emails actioned.")
            break
        summaries = classify_emails(messages, con)
        non_keep = [s for s in summaries if s.action != "keep"]
        if not non_keep:
            print(f"Pass {run_count}: {len(summaries)} emails, all keep. Inbox is clean.")
            break
        execute_actions(service, summaries, con, label_map)
        total_actioned += len(non_keep)
        keep_count = len(summaries) - len(non_keep)
        print(
            f"Pass {run_count}: actioned {len(non_keep)} ({total_actioned} total), {keep_count} kept. Continuing..."
        )


if __name__ == "__main__":
    import sys

    con = init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stage"

    if cmd in ("stage", "run"):
        print(stage())
    elif cmd == "execute":
        print(cmd_execute())
    elif cmd == "cancel":
        print(cmd_cancel())
    elif cmd == "pending":
        print(cmd_pending())
    elif cmd == "adjust":
        sender = sys.argv[2] if len(sys.argv) > 2 else ""
        action = sys.argv[3] if len(sys.argv) > 3 else ""
        print(cmd_adjust(sender, action))
    elif cmd == "purge_archive":
        purge_archive()
    elif cmd == "drain_categories":
        drain_categories()
    elif cmd == "drain":
        drain()
    elif cmd == "review":
        print(review_pending(con))
    elif cmd == "confirm_all":
        print(confirm_all(con))
    elif cmd == "confirm_action":
        action = sys.argv[2] if len(sys.argv) > 2 else ""
        print(confirm_action(con, action))
    elif cmd == "override":
        sender = sys.argv[2] if len(sys.argv) > 2 else ""
        action = sys.argv[3] if len(sys.argv) > 3 else ""
        print(override_rule(con, sender, action))
    elif cmd == "heartbeat":
        print(cmd_heartbeat())
    elif cmd == "digest":
        print(cmd_digest())
    elif cmd == "body":
        msg_id = sys.argv[2] if len(sys.argv) > 2 else ""
        print(cmd_body(msg_id))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
