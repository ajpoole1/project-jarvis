# finance

Personal CFO skill — stdlib-only, data-driven, backed by SQLite. Tracks accounts,
transactions, recurring charges, and proactive cashflow signals. Runs without any
third-party dependencies or network access.

## Architecture

```
Bank CSV exports   →  csv_import.py  →  db.py (finance.db)  →  skill.py commands
Dropbox inbox     →  csv_import.py ↗                        →  brief.py (morning-briefing)
Plaid API         →  plaid_sync.py ↗   finance_rules table  →  salience.py (proactive signals)
```

**Dropbox inbox:** `/mnt/c/Users/aaron/jarvis-finance/inbox/` — drop CSV exports here,
run `finance ingest`, they are parsed, imported, rule-matched, and archived automatically.

**Plaid sync:** live transaction pull for connected institutions. Run `finance plaid-sync`
to fetch new/modified/removed transactions since the last call. Cursors are persisted in
`plaid_cursors` so each run is incremental. See [Plaid sync setup](#plaid-sync-setup) below.

## Account inventory

| Account | ID in DB | Type | Notes |
|---|---|---|---|
| RBC Personal Chequing | `rbc-04330-5118989` | chequing | Primary income/bill account |
| RBC Joint Chequing/Savings | `rbc-03280-5365176` | savings | Joint household spending |
| RBC Personal Savings | `rbc-04330-5120290` | savings | Personal savings |
| RBC Joint Savings | `rbc-04330-5215470` | savings | Joint savings |
| RBC Nomi (Find & Save) | `rbc-04330-5210174` | savings | Automated round-up savings |
| RBC Royal Credit Line (LOC) | `rbc-01458-07839006001` | loan | Line of credit |
| TD CC (1225) | `td-visa-1225` | credit | TD Visa card |
| TD Chequing (5116) | `td-chq-5116` | chequing | TD chequing |
| MBNA (8830) | `mbna-8830` | credit | MBNA Mastercard |
| Wealthsimple BDL Pension | `ws-WK19SSZN2CAD` | investment | AJ pension/RRSP |
| Wealthsimple Upgrade (RRSP) | `ws-WK2P9ZT49CAD` | investment | AJ RRSP |
| Wealthsimple Ellie's RESP | `ws-WK3QRZ8Q0CAD` | investment | Ellie RESP |
| Wealthsimple Joint Cash | `ws-WK40VYS33CAD` | savings | Joint cash account |
| Wealthsimple Crypto | `ws-HQ6YWF114CAD` | investment | Crypto (non-CAD rows skipped) |
| Desjardins PCA (710490-PCA) | `dsj-pca-710490` | chequing | Mortgage clearing account |
| Desjardins LN1 (710490-LN1) | `dsj-ln1-710490` | mortgage | Mortgage — $562,984.65 current, 4.990%, term ends 2027-09-10 |

## Data ingestion

### Dropbox inbox (recommended)

```bash
# Drop CSVs into the inbox, then:
python skill.py ingest           # sweep inbox, import all, archive successes
python skill.py ingest --no-discord   # suppress Discord summary

# TD files have no account number embedded — prefix the filename:
#   td-chq-5116-june.csv     → account td-chq-5116
#   td-visa-1225-may.csv     → account td-visa-1225
```

**Inbox layout:**
```
/mnt/c/Users/aaron/jarvis-finance/inbox/
    *.csv                  ← drop exports here
    archive/               ← imported files land here (timestamp-prefixed)
    failed/                ← unrecognised or ambiguous files quarantined here
```

### Manual import

```bash
python skill.py import path/to/rbc.csv
python skill.py import path/to/mbna.csv --account mbna-8830 --owner personal
python skill.py import-holdings path/to/holdings-report-2026-06-18.csv
```

## Classification rules (finance_rules)

Transactions are classified via a priority-ordered rule table. Higher priority wins;
first match per transaction stops evaluation.

```bash
# Add a rule
python skill.py rule set --merchant "%DIGITALOCEAN%" --category infrastructure --owner altaforma --priority 10

# Rule with sub-vs-usage disambiguation (Anthropic: subscription vs API)
python skill.py rule set --merchant "%ANTHROPIC%CLAUDE%" --recurrence fixed_monthly --category subscriptions --owner personal --priority 20
python skill.py rule set --merchant "%ANTHROPIC%" --category software --owner altaforma --priority 10

# List / remove
python skill.py rule list
python skill.py rule list --merchant ANTHROPIC
python skill.py rule rm 3
```

## Tag + generalise (WS3 correction loop)

```bash
# Tag one transaction and immediately propose a generalising rule
python skill.py tag txn-001 altaforma --category infrastructure --rule

# Confirm the proposal (writes rule + back-applies to all historical matches)
python skill.py tag --confirm-rule 1

# Discard without writing
python skill.py tag --discard-rule 1
```

## Analysis commands

```bash
python skill.py accounts               # grouped table with balances
python skill.py liquid                 # net liquid + mortgage-buffer gap
python skill.py spend                  # current month by category
python skill.py spend --week
python skill.py spend --from 2026-05-01 --to 2026-05-31
python skill.py spend --owner altaforma
python skill.py top                    # top 10 merchants this month
python skill.py net                    # income vs spend net (current month)
python skill.py compare                # this month vs 3-month median per category
python skill.py compare --week         # vs 8-week median
python skill.py search AMAZON
python skill.py recurring              # detected recurring charges
python skill.py bills-due --days 14   # upcoming obligations vs chequing
python skill.py debt                   # full debt stack with projected payoff
python skill.py runway                 # liquid / monthly obligations in months
python skill.py altaforma              # Altaforma YTD spend/revenue
python skill.py subs-audit             # subscription NEW / MISSING / CHANGED
python skill.py apply-rules            # re-apply all finance_rules to history
```

## Setup

No venv required — stdlib only.

```bash
# Set in ~/.jarvis.env
JARVIS_DATA_DIR=/mnt/c/Users/aaron/Documents/python/project-jarvis/data
```

## Plaid sync setup

Requires a Plaid production account. Add to `~/.jarvis.env`:

```bash
PLAID_CLIENT_ID=<from dashboard.plaid.com/developers/keys>
PLAID_SECRET=<production secret>
PLAID_ACCESS_TOKEN_<LABEL>=access-production-...   # one per connected institution
PLAID_ITEM_ID_<LABEL>=...                          # optional, informational
```

`<LABEL>` is a short uppercase identifier (e.g. `TD_VISA`). Multiple institutions are
supported — add one `PLAID_ACCESS_TOKEN_*` entry per item.

To connect a new institution, run the Plaid quickstart at `/home/ajpoole/plaid-quickstart/`,
complete the Link flow, then capture the access token from `/api/info` and append it to
`~/.jarvis.env`.

**DB tables created by plaid_sync:**

| Table | Purpose |
|---|---|
| `plaid_cursors` | Per-label sync cursor; enables incremental pulls |
| `plaid_account_map` | Maps Plaid `account_id` → internal DB account ID |

**Source handoff:** once Plaid covers an account, delete the overlapping CSV rows to avoid
duplicates. Plaid rows use `source='plaid'` and `id='plaid-<transaction_id>'`; CSV rows
use `source='csv'` and a SHA256-based ID.

## Modules

| File | Purpose |
|---|---|
| `csv_import.py` | RBC / TD / MBNA / Desjardins / Wealthsimple / Rogers CSV parser with SHA256 dedup |
| `db.py` | Schema init, upsert helpers, finance_rules engine, recurring detection, tag_proposals |
| `plaid_sync.py` | Plaid `transactions/sync` pull — cursor persistence, account mapping, DB upsert |
| `brief.py` | `finance_brief()` — importable snapshot for morning-briefing (includes salience_block) |
| `salience.py` | `compute_salience_block()` — 5 proactive signals: cashflow, irregular charges, anomalies, sub drift, duplicates |
| `alerts.py` | 3 P1 alerts: large unusual charge, bill shortfall, duplicate charge |
| `skill.py` | CLI entry point (argparse + cmd_* dispatch) |

## CSV formats supported

**RBC:** `Account Type,Account Number,Transaction Date,Cheque Number,Description 1,Description 2,CAD$,USD$` — sign convention correct (negative = spend). Multiple accounts per file; LOC interest in Description 2 when CAD$ is 0.00.

**MBNA:** `Date,Transaction,Name,Memo,Amount` — purchases positive in export, negated to match convention.

**Rogers:** `Transaction Date,Post Date,Description,Category,Card Number,Credit,Debit` — `amount = credit - debit`.

**TD CC:** No header, 5 cols: `date(MM/DD/YYYY), description, charge, payment, balance`. `amount = payment - charge`. Requires filename hint for account ID.

**TD Chequing:** No header, 5 quoted cols: `date(YYYY-MM-DD), description, debit, credit, balance`. `amount = credit - debit`. Requires filename hint for account ID.

**Desjardins:** No header, 14 cols. PCA = chequing rows; LN1 = mortgage rows with principal/interest breakdown in description.

**Wealthsimple:** `date, transaction, description, amount, balance, currency`. Account ID from filename code (e.g. `WK19SSZN2CAD`). Non-CAD rows skipped.

## Amount sign convention

```
negative = money leaving  (purchases, fees, mortgage payments)
positive = money arriving (salary, CC payment received, refunds)

SUM(amount) < 0  →  you spent more than came in
```

## Proactive signals (salience_block)

Available in `finance_brief()['salience_block']` every morning:

| Signal | What it catches |
|---|---|
| `cashflow_projection` | 90-day forward runway (income − obligations) |
| `large_charge_forecast` | Quarterly/annual charges ≥$50 due within 60 days |
| `category_anomalies` | Categories >20% above 3-month median this month |
| `subscription_drift` | New / missing / stable subscription counts |
| `duplicate_charges` | Same merchant + amount within 3 days |

## Finance rules priority model

```
match_merchant  — SQL LIKE pattern (% = any, _ = one char)   required
match_descriptor — substring in description                   optional
match_recurrence — fixed_monthly | variable | annual | …      optional
match_amount_min/max — abs(amount) range (no exact match)    optional
priority        — higher wins; first match per txn stops      required
```

Seeded rules (on DB init): transfer/internal patterns, common spend categories
(groceries, gas, dining, utilities), Altaforma vendor rules (DigitalOcean, Cloudflare,
AWS, GitHub), Anthropic subscription vs API disambiguation.

## Credit card strategy

Points harvest, pay-in-full discipline. Statement close: TD Visa ~20th, MBNA ~17th.
Payment due ~15th of following month. Natural payment windows: Polina's EoM deposit
or AJ's first Friday of the month.

## Constants

- `MORTGAGE_BUFFER_GOAL = 2140` CAD — one Desjardins payment; target chequing floor
- **Buffer account:** hold the floor in TD Chequing, not Desjardins
- **Desjardins PCA:** credit union dividends earmarked for hot water tank — exclude from liquid totals

## Testing

```bash
python3 -m pytest tests/test_finance.py -v
```

No live API calls. 81 unit tests covering DB, CSV import, recurring detection, alerts,
brief, finance_rules engine, ingest, tag correction loop, and salience block signals.
