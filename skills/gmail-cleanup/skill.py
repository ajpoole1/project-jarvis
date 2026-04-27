"""Gmail cleanup skill — classifies and stages inbox actions for user approval."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
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
    con.commit()
    return con


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


def classify_emails(emails: list[dict], con: sqlite3.Connection) -> list[EmailSummary]:
    client = anthropic.Anthropic()
    results = []

    uncached = []
    for msg in emails:
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")
        name, email = parse_sender(raw_from)

        cached = get_cached_action(con, email)
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
            uncached.append((msg["id"], name, email, subject))

    CLASSIFY_CHUNK = 50
    PROMPT_TEMPLATE = """Classify each email as one of: archive, trash, unsubscribe, keep.

Rules:
- keep: personal correspondence from real people, financial alerts (low balance, fraud, CRA/tax), health/medical, travel bookings, anything related to the user's daughter Ellie or wife Polina
- archive: invoices, receipts, and billing statements from non-Amazon vendors (banks, insurance, software, professional services), job applications, account statements
- trash: ALL Amazon order confirmations and shipping notifications, ALL Shopify merchant shipping/delivery emails, any order or tracking email from a retail store or marketplace
- unsubscribe: marketing/promotional email, retail sale announcements, newsletters the user did not explicitly request
- trash: spam, irrelevant bulk mail, duplicate notifications, automated alerts with no action required, ANY email from a retailer or vendor that does not contain a specific order number, tracking number, or account-specific transaction detail — generic "sale", "new arrivals", "don't miss out" emails from stores are always trash even if the store is known

PRIORITY RULES:
- message@amisgest.com with subject containing "journal de bord": trash — user gets app notifications, email is redundant
- message@amisgest.com (all other subjects): keep — caregiver messages and important school communications
- Any email referencing "Ellie", "Polina Poole", or "Polina Serebryakova" (user's daughter and wife): always keep

Also assign a tag from: receipts, bills, job-search, health, family, projects, none

Respond with a JSON array, one object per email, in the same order:
[{{"action": "archive", "tag": "receipts", "reason": "brief reason"}}, ...]

Emails:
{batch_input}"""

    for chunk_start in range(0, len(uncached), CLASSIFY_CHUNK):
        chunk = uncached[chunk_start : chunk_start + CLASSIFY_CHUNK]
        batch_input = "\n".join(
            f'{i+1}. From: "{name}" <{email}> | Subject: {subject}'
            for i, (_, name, email, subject) in enumerate(chunk)
        )
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(batch_input=batch_input)}],
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

        for (msg_id, name, email, subject), cls in zip(chunk, classifications, strict=False):
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
                )
            )
            cache_rule(con, email, action, confirmed=False)

    return results


def build_staging_report(summaries: list[EmailSummary], dry_run: bool) -> str:
    grouped: dict[str, list[EmailSummary]] = {a: [] for a in ACTIONS}
    for s in summaries:
        grouped[s.action].append(s)

    lines = [f"**Gmail cleanup — {'DRY RUN ' if dry_run else ''}staged actions**\n"]
    for action in ("trash", "unsubscribe", "archive", "keep"):
        items = grouped[action]
        if not items:
            continue
        lines.append(f"**{action.upper()} ({len(items)})**")
        for item in items[:10]:
            lines.append(f"  • {item.sender_email} — {item.subject[:60]}")
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


def run(batch_size: int = DEFAULT_BATCH_SIZE, dry_run: bool = DRY_RUN) -> str:
    """Entry point called by OpenClaw. Returns a staging report string."""
    service = get_gmail_service()
    con = init_db()
    label_map = get_or_create_labels(service)
    messages = fetch_inbox_messages(service, batch_size)
    summaries = classify_emails(messages, con)
    report = build_staging_report(summaries, dry_run)
    if not dry_run:
        non_keep = [s for s in summaries if s.action != "keep"]
        execute_actions(
            service, non_keep + [s for s in summaries if s.action == "keep"], con, label_map
        )
    return report


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
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "purge_archive":
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
    else:
        print(run())
