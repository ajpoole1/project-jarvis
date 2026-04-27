# gmail-cleanup

Classifies inbox emails and stages actions for user approval before executing.

## What it does

1. Fetches up to `GMAIL_BATCH_SIZE` inbox emails (default: 50)
2. Checks SQLite for cached sender rules — skips Haiku call for known senders
3. Classifies unknown senders with `claude-haiku` (archive / trash / unsubscribe / keep)
4. Returns a staged report in Discord
5. If `GMAIL_DRY_RUN=false`, executes non-keep actions immediately (use with caution)

The stage-then-approve flow is enforced at the OpenClaw layer — this skill only executes
when called with `dry_run=False` after explicit user confirmation.

## Setup

### 1. Google Cloud credentials

1. Go to console.cloud.google.com → create a project
2. Enable the **Gmail API**
3. Create **OAuth 2.0 credentials** → Desktop app → download JSON
4. Rename to `gmail_credentials.json` and place in `/config/personal/`

### 2. Environment variables

| Variable | Default | Description |
|---|---|---|
| `GMAIL_BATCH_SIZE` | `50` | Emails to process per run |
| `GMAIL_DRY_RUN` | `true` | Set to `false` to execute actions |
| `JARVIS_DATA_DIR` | `/data` | Path to SQLite DB directory |
| `JARVIS_CONFIG_DIR` | `/config/personal` | Path to credentials directory |

### 3. First run (OAuth flow)

```bash
cd skills/gmail-cleanup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python skill.py
```

The first run opens a browser for Gmail OAuth consent. Token is saved to
`/config/personal/gmail_token.json` — gitignored.

## SQLite schema

Table: `gmail_sender_rules`

| Column | Type | Description |
|---|---|---|
| `sender_email` | TEXT PK | Normalized sender address |
| `action` | TEXT | archive / trash / unsubscribe / keep |
| `confirmed` | INTEGER | 1 = user confirmed, 0 = Haiku suggestion |
| `last_applied` | TEXT | ISO datetime of last application |

Only `confirmed=1` rules are used for cache hits on future runs.
