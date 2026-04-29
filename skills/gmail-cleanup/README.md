# gmail-cleanup

Classifies inbox emails using Claude Haiku and stages actions for explicit user approval before executing. Nothing is deleted or archived without a confirm step.

## What it does

1. Fetches inbox emails (incremental in heartbeat mode — only since last check)
2. Checks SQLite for confirmed sender rules — bypasses Haiku for known senders
3. Passes email snippet to Haiku for all uncached emails — subject + body preview
4. Classifies unknown senders with Claude Haiku: `archive / trash / unsubscribe / keep`
5. Sets `calendar_hint: true` when an email contains appointment/booking info not yet on calendar
6. Optionally injects upcoming Google Calendar events so appointment emails already saved are archived
7. Saves staged actions to `gmail_pending_actions` SQLite table — **nothing executes yet**
8. Returns a formatted report to Discord
9. Waits for explicit `execute` command before touching any email

## Stage-then-approve flow

```
stage → [review / adjust] → execute
                          └→ cancel (guaranteed no-op)
```

The two-step flow is enforced at the **skill level**, not just by agent instructions.

## Commands

### Scheduled (run by cron via `scripts/` — do not call manually)

| Command | Description |
|---|---|
| `python skill.py heartbeat` | Incremental check since `last_checked`. Outputs `SILENT`, `IMMEDIATE: ...`, or `DIGEST ADDED: ...`. First run initialises timestamp only. |
| `python skill.py digest` | Posts queued non-priority emails as a rollup and clears the queue. |

### Manual cleanup

| Command | Description |
|---|---|
| `python skill.py stage` | Classify inbox, save to pending, show report |
| `python skill.py execute` | Apply all pending actions |
| `python skill.py cancel` | Discard pending actions — nothing changes |
| `python skill.py pending` | Show current staged actions |
| `python skill.py adjust <email> <action>` | Change action for a specific sender before executing |
| `python skill.py drain` | Fully automated: classify and execute in a loop until inbox is clean |
| `python skill.py drain_categories` | Drain Gmail category tabs (Promotions, Updates, Social, Forums) |
| `python skill.py purge_archive` | Trash archived emails from confirmed trash/unsubscribe senders |

### Body fetch

| Command | Description |
|---|---|
| `python skill.py body <msg_id>` | Fetch plain-text body of an email by message ID. Use when subject alone is ambiguous — e.g. appointment reminders without date/time in subject. |

### Rule management

| Command | Description |
|---|---|
| `python skill.py review` | Show unconfirmed sender rules in SQLite |
| `python skill.py confirm_action <action>` | Confirm all unconfirmed rules for a given action |
| `python skill.py confirm_all` | Confirm all unconfirmed sender rules |
| `python skill.py override <email> <action>` | Manually set a confirmed sender rule |

## Heartbeat priority detection

The `heartbeat` command splits results into two tiers:

**Immediate ping** (posts to Discord right away):
- Tag is `family`
- `calendar_hint: true` — appointment/booking with date in body not subject
- Subject contains security keywords: unauthorized, breach, login attempt, etc.
- Subject contains financial keywords: low balance, fraud alert, CRA, etc.
- Action is `keep` and sender doesn't look automated (real person)

**Digest queue** (accumulated, posted at 13:00 and 18:00 by cron):
- All other actionable emails: trash, archive, unsubscribe

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
| `confirmed` | INTEGER | 1 = user-confirmed, 0 = Haiku suggestion |
| `last_applied` | TEXT | ISO datetime of last application |

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

### `gmail_heartbeat_state`
| Column | Type | Description |
|---|---|---|
| `key` | TEXT PK | `last_checked` or `digest_queue` |
| `value` | TEXT | ISO timestamp or JSON array of queued items |

## Personal classification rules

Subject-conditional sender rules, always-keep names, and `NEVER_CACHE_SENDERS` live in `JARVIS_CONFIG_DIR/gmail_rules.json` — gitignored.

Copy `config/examples/gmail_rules.json` as a starting point. If the personal file is absent the skill falls back to the example file.

## Tagging

Applied Gmail labels: `jarvis/receipts`, `jarvis/bills`, `jarvis/job-search`, `jarvis/health`, `jarvis/family`, `jarvis/projects`. Created automatically on first run.

## Calendar integration

If `skills/calendar` is installed, the skill fetches the next 30 days of calendar events and injects them into the Haiku prompt. Appointment emails for events already on calendar are archived. Appointment emails not yet on calendar get `calendar_hint: true` and surface as IMMEDIATE in the heartbeat.
