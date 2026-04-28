# gmail-cleanup

Classifies inbox emails using Claude Haiku and stages actions for explicit user approval before executing. Nothing is deleted or archived without a confirm step.

## What it does

1. Fetches up to `GMAIL_BATCH_SIZE` inbox emails
2. Checks SQLite for confirmed sender rules — bypasses Haiku for known senders
3. Classifies unknown senders with Claude Haiku: `archive / trash / unsubscribe / keep`
4. Optionally injects upcoming Google Calendar events so appointment confirmation emails already saved to calendar are archived automatically
5. Saves staged actions to `gmail_pending_actions` SQLite table — **nothing executes yet**
6. Returns a formatted report to Discord
7. Waits for explicit `execute` command before touching any email

## Stage-then-approve flow

```
stage → [review / adjust] → execute
                          └→ cancel (guaranteed no-op)
```

The two-step flow is enforced at the **skill level**, not just by agent instructions. There is no way to execute without a separate `execute` call.

## Commands

| Command | Description |
|---|---|
| `python skill.py` / `python skill.py stage` | Classify inbox, save to pending, show report |
| `python skill.py execute` | Apply all pending actions |
| `python skill.py cancel` | Discard pending actions — nothing changes |
| `python skill.py pending` | Show current staged actions |
| `python skill.py adjust <email> <action>` | Change action for a specific sender before executing |
| `python skill.py drain` | Fully automated: classify and execute in a loop until inbox is clean |
| `python skill.py drain_categories` | Drain Gmail category tabs (Promotions, Updates, Social, Forums) |
| `python skill.py purge_archive` | Trash archived emails from confirmed trash/unsubscribe senders |
| `python skill.py review` | Show unconfirmed sender rules in SQLite |
| `python skill.py confirm_action <action>` | Confirm all unconfirmed rules for a given action |
| `python skill.py confirm_all` | Confirm all unconfirmed sender rules |
| `python skill.py override <email> <action>` | Manually set a confirmed sender rule |

## Setup

### 1. Google Cloud credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project
2. Enable the **Gmail API**
3. Create **OAuth 2.0 credentials** (Desktop app) → download JSON
4. Rename to `gmail_credentials.json` and place in `JARVIS_CONFIG_DIR`

### 2. Environment variables

| Variable | Default | Description |
|---|---|---|
| `GMAIL_BATCH_SIZE` | `50` | Emails to process per run |
| `JARVIS_DATA_DIR` | `/data` | Path to SQLite DB directory |
| `JARVIS_CONFIG_DIR` | `/config/personal` | Path to credentials directory |

### 3. Virtualenv and first run

```bash
cd skills/gmail-cleanup
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
python skill.py stage
```

The first run opens a browser for Gmail OAuth consent. Token saved to `JARVIS_CONFIG_DIR/gmail_token.json` — gitignored.

## SQLite schema

### `gmail_sender_rules`

| Column | Type | Description |
|---|---|---|
| `sender_email` | TEXT PK | Normalized sender address |
| `action` | TEXT | archive / trash / unsubscribe / keep |
| `confirmed` | INTEGER | 1 = user-confirmed rule, 0 = Haiku suggestion |
| `last_applied` | TEXT | ISO datetime of last application |

Only `confirmed=1` rules are used as cache hits. Senders in `NEVER_CACHE_SENDERS` always go through Haiku regardless.

### `gmail_pending_actions`

| Column | Type | Description |
|---|---|---|
| `msg_id` | TEXT PK | Gmail message ID |
| `sender_email` | TEXT | Normalized sender address |
| `sender_display` | TEXT | Display name from From header |
| `subject` | TEXT | Email subject |
| `action` | TEXT | Staged action |
| `tag` | TEXT | jarvis/* label to apply |
| `reason` | TEXT | Haiku's classification reason |
| `staged_at` | TEXT | ISO datetime of staging |

Cleared on each `stage` run and on `execute` / `cancel`.

## Tagging

Applied Gmail labels: `jarvis/receipts`, `jarvis/bills`, `jarvis/job-search`, `jarvis/health`, `jarvis/family`, `jarvis/projects`. Labels are created automatically on first run if they don't exist.

## Calendar integration

If `skills/calendar` is installed and its venv exists, the skill fetches the next 30 days of calendar events and injects them into the Haiku prompt. Appointment confirmation or reminder emails for events already on the calendar are archived rather than kept.
