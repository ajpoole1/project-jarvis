# finance

Personal CFO skill backed by Wealthica (Canadian bank aggregator) + SQLite. Tracks accounts, transactions, recurring charges, and push alerts. Separate from `jarvis.db` — lives in `finance.db`.

## Architecture

```
Wealthica REST API  →  wealthica.py  →  db.py (finance.db)  →  skill.py commands
CSV dumps (MBNA)    →  csv_import.py ↗                      →  brief.py (for morning-briefing)
                                                             →  alerts.py (fires on sync)
```

## Setup

No third-party dependencies — stdlib only.

```bash
# Store credentials in ~/.jarvis.env (never in the repo)
WEALTHICA_CLIENT_ID=xxxx
WEALTHICA_SECRET=xxxx
WEALTHICA_USER=your@email.com
JARVIS_DATA_DIR=/data   # finance.db is created here
```

Without `WEALTHICA_CLIENT_ID`, the skill runs in **mock mode** and returns realistic fixture data — safe for testing.

## Subcommands

### Data ingestion

```bash
python skill.py sync                      # sync last period (default: since last sync or 90d)
python skill.py sync --since 2026-01-01   # explicit start date
python skill.py sync --full               # full history from 2020-01-01
python skill.py import mbna.csv --account acct-mbna --owner personal
python skill.py import rogers.csv --owner altaforma
```

### Account overview

```bash
python skill.py accounts     # grouped table: bank | credit | investment | loan + totals
python skill.py liquid       # net liquid position + mortgage-buffer goal gap
```

### Spend analysis

```bash
python skill.py spend                               # current month by category
python skill.py spend --week                        # current week
python skill.py spend --from 2026-05-01 --to 2026-05-31
python skill.py spend --owner altaforma             # Altaforma expenses only
python skill.py top                                 # top 10 merchants this month
python skill.py top 5 --period week
python skill.py net                                 # income vs spend vs net (current month)
python skill.py net --from 2026-05-01
```

### Searching and tagging

```bash
python skill.py search AMAZON        # LIKE match on description + note
python skill.py tag txn-001 altaforma --category infrastructure
```

### Recurring + bills

```bash
python skill.py recurring            # table of detected recurring charges
python skill.py bills-due            # obligations due in next 7 days vs chequing
python skill.py bills-due --days 14
```

## Environment variables

| Var | Required | Description |
|---|---|---|
| `WEALTHICA_CLIENT_ID` | Yes (for live sync) | Wealthica personal developer client ID |
| `WEALTHICA_SECRET` | Yes (for live sync) | Wealthica developer secret |
| `WEALTHICA_USER` | Yes (for live sync) | Wealthica account email |
| `JARVIS_DATA_DIR` | No (default `/data`) | Directory for `finance.db` |

## Modules

| File | Purpose |
|---|---|
| `wealthica.py` | REST client + mock fixture mode |
| `csv_import.py` | MBNA / Rogers CSV parser with SHA256 dedup |
| `db.py` | Schema init, upsert helpers, recurring detection |
| `brief.py` | `finance_brief()` — standalone-importable snapshot for morning-briefing |
| `alerts.py` | 3 P1 alerts: large charge, bill shortfall, duplicate charge |
| `skill.py` | CLI entry point (argparse, dispatches to cmd_* functions) |

## CSV formats supported

**MBNA:** `Date,Transaction,Name,Memo,Amount` — purchases are positive in export, negated to match our convention.

**Rogers:** `Transaction Date,Post Date,Description,Category,Card Number,Credit,Debit` — Credit column = money in (positive), Debit column = money out (negative).

## Amount sign convention

```
negative = money leaving  (purchases, fees, mortgage payments)
positive = money arriving (salary, CC payment received, refunds)

SUM(amount) < 0  →  you spent more than came in
```

## Push alerts (fired on sync)

Three P1 alerts written to `sync_state.pending_alerts` JSON:
- **large_unusual_charge** — amount > $200 AND (new merchant OR > 2σ above category norm)
- **bill_shortfall** — recurring bill due in 3 days AND amount > chequing balance
- **duplicate_charge** — same merchant + same amount within 48h

Discord delivery is handled by the morning-briefing layer.

## Constants

- `MORTGAGE_BUFFER_GOAL = 2140` CAD — one Desjardins mortgage payment; the target chequing floor

## Gate to live sync

Wealthica API credentials must be received from `hello@wealthica.com` and stored in `~/.jarvis.env` before first live sync. Mock mode works immediately for development.

## Testing

```bash
python3 -m pytest tests/test_finance.py -v
```

No live API calls. 41 unit tests covering DB, CSV import, recurring detection, alerts, brief, and all P1 commands.
