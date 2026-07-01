# gmail-cleanup

Intent-keyed Gmail classifier. Sorts inbox into three salience tiers (Act/Aware/Archive), applies a reversible four-rung disposition ladder, and never executes without an explicit confirm step. All behaviour is controlled by data in `jarvis.db` — no code changes needed to tune decisions.

## Model (2026-0041)

### Three salience tiers

| Tier | Meaning | Default surface |
|---|---|---|
| **Act** | Needs AJ — open loop | Inbox (immediate ping or silent, per `gmail_ping_rules`) |
| **Aware** | Not urgent, not noise — worth knowing | Named line in daily ledger only |
| **Archive** | Pure noise | Silently filed or removed; count in ledger |

### Four-rung disposition ladder

| Disposition | What happens | Reversible? |
|---|---|---|
| `inbox` | Stays in INBOX, `jarvis/<tag>` label applied | Yes (via drag/delete) |
| `file` | Removed from INBOX, `jarvis/<tag>` applied | Yes (in All Mail) |
| `quarantine` | Removed from INBOX, `jarvis/quarantine` applied | **Yes** — AJ drags back to trigger correction |
| `trash_direct` | Gmail trash immediately | No — **AJ-authored only**, never an autonomous guess |

### Autonomy gating

Autonomous moves (no staging prompt) require **all** of:
- `confidence >= gmail_config.autonomy_threshold` (default 0.85)
- `disposition ∈ {file, quarantine}` (reversible only)
- `needs_aj = false`
- `uncertain = false`

Below threshold → staged for AJ approval. Raise threshold as trust grows:
```
gmail config set autonomy_threshold 0.90
```

### Classification is intent-keyed, not sender-keyed

Per message, Haiku answers: *what type is this?* Type→tier/disposition/needs_aj/ping rules live in `gmail_type_rules`. Sender is a *signal* (plus explicit overrides in `gmail_senders`). No hardcoded senders, tiers, or thresholds in code.

The worked example: **Petit Parchemin (daycare app)**

| Message | Type key | Tier | Disposition | Ping? |
|---|---|---|---|---|
| Daily journal de bord | `daycare_routine` | archive | trash_direct | no |
| Monthly bulletin | `bulletin` | aware | file | no |
| Staff message / incident | `daycare_message` | act | inbox | yes |

One sender → three different dispositions. Impossible with a sender-keyed cache.

## Commands

### Scheduled (dispatched by heartbeat — do not call manually)

| Command | Schedule | Description |
|---|---|---|
| `python skill.py heartbeat` | every 10 min | Incremental check since `last_checked`. Posts `SILENT`, `IMMEDIATE`, or reconciliation alerts. |
| `python skill.py ledger` | daily@12:00 | Named Aware-tier items + autonomous move count for trust-building period. |

### Manual cleanup

| Command | Description |
|---|---|
| `python skill.py stage` | Classify inbox, save to pending, show staged report grouped by disposition |
| `python skill.py execute` | Apply all pending staged actions |
| `python skill.py cancel` | Discard pending actions — nothing changes |
| `python skill.py pending` | Show current staged actions (each line ends with `·id <msg_id>`) |
| `python skill.py adjust <email\|msg_id> <action>` | Change staged action. A sender email changes all their staged mail; a `msg_id` (from the `·id` in the report) changes exactly one entry — use it to disambiguate items sharing a sender+subject |
| `python skill.py digest` | Full inbox status report grouped by tier (ACT/AWARE/ARCHIVE). Read-only. |
| `python skill.py body <msg_id>` | Fetch plain-text body of an email — use when subject is ambiguous |
| `python skill.py paths` | Print the resolved `jarvis.db` path, its source (`JARVIS_DATA_DIR`/.jarvis.env vs `/data` fallback), and whether it exists |

### Decision layer — data, not code

All behaviour tuning is a staged DB write. No branch, no PR, no git:

```
gmail sender set <pattern> --tier <act|aware|archive> [--bypass trash_direct|always_inbox]
gmail sender list
gmail sender remove <pattern>

gmail type set <type> --tier <t> --disposition <d> [--ping on|off] [--needs-aj on|off]
gmail type list
gmail type remove <type>

gmail ping set <type> on|off
gmail ping list

gmail config set <key> <value>
gmail config list

gmail rules show           # one-screen summary of all four tables
```

Changes take effect on the next heartbeat run.

### Backlog drain (destructive — explicit gate)

```
python skill.py backlog-drain --i-understand              # drain inbox
python skill.py backlog-drain --i-understand --categories # drain category tabs
```

Auto-executes without approval. Only for large backlog clearing events.

### Watch rules

Named semantic watch rules checked on every heartbeat.

| Command | Description |
|---|---|
| `python skill.py watch add <label> "<description>"` | Add a new watch rule |
| `python skill.py watch list` | List all active rules |
| `python skill.py watch remove <id>` | Remove a rule |
| `python skill.py watch pause <id>` / `resume <id>` | Pause / re-activate |

### Archive expiry

Per-tag retention. `expire run` trashes archived emails older than their retention period.

| Command | Description |
|---|---|
| `python skill.py expire set <tag> <days>` | Set retention period |
| `python skill.py expire list` | Show all policies |
| `python skill.py expire preview` | Dry run |
| `python skill.py expire run` | Execute purge |

### Flagged emails

Emails where the classifier sets `uncertain=true` are saved to `gmail_flagged` and surfaced as IMMEDIATE in heartbeat.

| Command | Description |
|---|---|
| `python skill.py flag list` | Show flagged emails with IDs and reason |
| `python skill.py flag decide <id> <action>` | Execute action and clear |
| `python skill.py flag clear` | Discard without deciding |

### None-queue drain

`jarvis/none` is a transient marker for emails awaiting tier-2 (body-level) classification.

| Command | Description |
|---|---|
| `python skill.py drain-none` | Body-classify all emails in the none queue |

## Correction feedback loop

When AJ drags a quarantined item back to the inbox, the next heartbeat detects it and surfaces:
- A summary of the correction
- A proposed `gmail_type_rules` update (`gmail type set <type> --tier act --needs-aj on`)

Staged then ✅ — the fix attaches to the *type*, not the sender, so it generalizes.

Repeated reversals of the same type retire that type's autonomous gate.

## Setup

### Google Cloud credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project
2. Enable the **Gmail API**
3. Create **OAuth 2.0 credentials** (Desktop app) → download JSON
4. Rename to `gmail_credentials.json` and place in `JARVIS_CONFIG_DIR`
5. Set publishing status to **Production** — Testing mode expires refresh tokens after 7 days

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `GMAIL_BATCH_SIZE` | `50` | Emails to process per run |
| `JARVIS_DATA_DIR` | `/data` | Path to SQLite DB directory |
| `JARVIS_CONFIG_DIR` | `/config/personal` | Path to credentials directory |

### First run

```bash
cd skills/gmail-cleanup
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
python skill.py stage
```

On first run, the skill prints an OAuth URL to the terminal (works in WSL2 headless). Open in Windows browser, grant access — token saved to `JARVIS_CONFIG_DIR/gmail_token.json`.

## SQLite schema (key tables)

### `gmail_type_rules` — decision layer (authoritative)
| Column | Type | Description |
|---|---|---|
| `type` | TEXT PK | Intent type key (e.g. `daycare_routine`, `unpaid_invoice`) |
| `tier` | TEXT | act / aware / archive |
| `disposition` | TEXT | inbox / file / quarantine / trash_direct |
| `needs_aj` | INTEGER | 1 = must stay in inbox |
| `ping` | INTEGER | 1 = interrupt immediately |
| `note` | TEXT | Human rationale |

Edit via `gmail type set`. Changes take effect on next heartbeat.

### `gmail_senders` — explicit sender overrides
| Column | Type | Description |
|---|---|---|
| `sender_pattern` | TEXT PK | Substring match against `From` address |
| `default_tier` | TEXT | Tier override for this sender |
| `bypass` | TEXT | `trash_direct` or `always_inbox` — skips classifier |

Edit via `gmail sender set`.

### `gmail_config` — thresholds and dials
| Key | Default | Description |
|---|---|---|
| `autonomy_threshold` | 0.85 | Min confidence for autonomous moves |
| `stage_threshold` | 0.60 | Below this → always stage |
| `quarantine_retain_days` | 30 | Auto-purge window for quarantine |
| `ledger_report_autonomous` | 1 | 1 = report autonomous moves in ledger |
| `none_queue_alarm_threshold` | 20 | Alert if jarvis/none queue exceeds this |

### `gmail_ping_rules` — Act-tier interrupt subset
| Column | Description |
|---|---|
| `match` | Type key (e.g. `personal`, `security`) |
| `ping` | 1 = interrupt, 0 = inbox silent |

### `gmail_pending_actions` — staged action queue
| Column | Description |
|---|---|
| `msg_id` | Gmail message ID |
| `action` | Legacy field (kept for schema compat) |
| `tag` | `jarvis/*` label to apply |
| `reason` | Classifier's rationale |

### `gmail_sender_rules` — **retired as decision primitive**
Kept in schema for rollback. No longer read for decisions. Signal migrated to `gmail_senders`.

### Other tables
`gmail_heartbeat_state`, `gmail_watches`, `gmail_expire_policies`, `gmail_flagged`, `gmail_tags` — see code.

## Calendar integration

If `skills/calendar` is installed, the skill fetches the next 30 days of events and injects them into the classifier prompt. Appointment emails for events already on calendar are filed. New appointments get `calendar_hint: true` and surface as IMMEDIATE in heartbeat.
