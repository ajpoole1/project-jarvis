# finance

Personal CFO skill backed by CSV imports + SQLite. Tracks accounts, transactions, recurring charges, and push alerts. Separate from `jarvis.db` — lives in `finance.db`.

## Architecture

```
RBC CSV exports     →  csv_import.py  →  db.py (finance.db)  →  skill.py commands
TD CSV exports      →  csv_import.py ↗                       →  brief.py (for morning-briefing)
MBNA CSV exports    →  csv_import.py ↗                       →  alerts.py (fires on sync)
Desjardins exports  →  csv_import.py ↗
```

## Account inventory

| Account | ID in DB | Type | Notes |
|---|---|---|---|
| RBC Personal Chequing | `rbc-04330-5118989` | chequing | Primary income/bill account |
| RBC Joint Chequing/Savings | `rbc-03280-5365176` | savings | Joint household spending |
| RBC Personal Savings | `rbc-04330-5120290` | savings | Personal savings |
| RBC Joint Savings | `rbc-04330-5215470` | savings | Joint savings |
| RBC Nomi (Find & Save) | `rbc-04330-5210174` | savings | Automated round-up savings |
| RBC Royal Credit Line (LOC) | `rbc-01458-07839006001` | loan | Line of credit |
| TD CC (1225) | `td-cc-1225` | credit | TD credit card |
| TD Chequing (5116) | `td-chq-5116` | chequing | TD chequing |
| MBNA (8830) | `mbna-8830` | credit | MBNA Mastercard |
| Wealthsimple BDL Pension | `ws-WK19SSZN2CAD` | investment | AJ pension/RRSP |
| Wealthsimple Upgrade (RRSP) | `ws-WK2P9ZT49CAD` | investment | AJ RRSP |
| Wealthsimple Ellie's RESP | `ws-WK3QRZ8Q0CAD` | investment | Ellie RESP |
| Wealthsimple Joint Cash | `ws-WK40VYS33CAD` | savings | Joint cash account |
| Wealthsimple Crypto | `ws-HQ6YWF114CAD` | investment | Crypto (BTC rows skipped — non-CAD) |
| Desjardins PCA (710490-PCA) | `dsj-pca-710490` | chequing | Mortgage clearing account — receives Polina e-transfers, pays LN1 |
| Desjardins LN1 (710490-LN1) | `dsj-ln1-710490` | mortgage | Mortgage loan — $587,600 original, $562,984.65 current balance, 4.990% annual, term ends 2027-09-10, amortization to 2049-06-10, payment $1,600.90 bi-monthly |

> Note: inter-account transfers appear as matching +/- pairs. A full account flow diagram is pending once all institutions are imported.

## Income schedule

| Person | Cadence | Day | Notes |
|---|---|---|---|
| AJ | Bi-weekly | Every second Friday | Next: 2026-06-19 |
| Polina | Semi-monthly | 15th + last day of month | Shifts to prior Friday if falls on weekend |

Cash flow planning: AJ's Friday deposit + Polina's 15th/EoM deposits are the primary liquidity events. Obligations due mid-month (mortgage clearing ~Jun 23, Hydro ~Jun 26, TD loan ~Jun 29) need one of these deposits to clear first.

## EoM top-up workflow (RBC)

RBC bulk export produces **two files** from the account history page:

1. **Chequing + Savings accounts** — all deposit accounts in one CSV
2. **LOC** — credit line in a separate CSV

```bash
# Run after each month-end download — dedup handles overlaps automatically
JARVIS_DATA_DIR=... python skill.py import "~/Downloads/rbc-accounts.csv"
JARVIS_DATA_DIR=... python skill.py import "~/Downloads/rbc-loc.csv"
```

No date filtering needed — import the full export every time.

## Setup

Requires Playwright for the scraper subcommand only. CSV import is stdlib-only.

```bash
# Create the skill venv and install Playwright
python3 -m venv skills/finance/.venv
skills/finance/.venv/bin/pip install -r skills/finance/requirements.txt
sudo skills/finance/.venv/bin/playwright install-deps chromium
skills/finance/.venv/bin/playwright install chromium

# Set in ~/.jarvis.env
JARVIS_DATA_DIR=/mnt/c/Users/aaron/Documents/python/project-jarvis/data
```

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
| `csv_import.py` | RBC / TD / MBNA / Desjardins / Wealthsimple CSV parser with SHA256 dedup |
| `db.py` | Schema init, upsert helpers, recurring detection |
| `brief.py` | `finance_brief()` — standalone-importable snapshot for morning-briefing |
| `alerts.py` | 3 P1 alerts: large charge, bill shortfall, duplicate charge |
| `scraper_base.py` | Playwright session lifecycle (first-auth + headless restore) |
| `scraper_rbc.py` | RBC transaction scraper (network intercept + CSV fallback) |
| `scraper_td.py` | TD transaction scraper (stub) |
| `skill.py` | CLI entry point (argparse, dispatches to cmd_* functions) |

## CSV formats supported

**RBC:** `Account Type,Account Number,Transaction Date,Cheque Number,Description 1,Description 2,CAD$,USD$` — sign convention already correct (negative = spend). Multiple account types and account numbers can appear in one file. LOC accounts encode interest/fee amounts in Description 2 when CAD$ is 0.00 — handled automatically.

**MBNA:** `Date,Transaction,Name,Memo,Amount` — purchases are positive in export, negated to match our convention.

**Rogers:** `Transaction Date,Post Date,Description,Category,Card Number,Credit,Debit` — Credit column = money in (positive), Debit column = money out (negative).

**TD CC:** No header, 5 cols: `date(MM/DD/YYYY), description, charge, payment, balance`. `amount = payment - charge`.

**TD Chequing:** No header, 5 quoted cols: `date(YYYY-MM-DD), description, debit, credit, balance`. `amount = credit - debit`.

**Desjardins:** No header, 14 cols. col[2]=account type (PCA/LN1), col[3]=date (YYYY/MM/DD), col[5]=description, col[7]=withdrawal, col[8]=deposit (PCA), col[9]=interest, col[12]=principal (LN1). LN1 rows stored as single combined row with breakdown in description.

**Wealthsimple:** `date, transaction, description, amount, balance, currency`. Amount sign already correct. Account ID derived from filename code segment (e.g. `WK19SSZN2CAD` → `ws-WK19SSZN2CAD`). Non-CAD rows (crypto) are skipped. Transaction types mapped to categories: DIV/INT → `investment_income`, BUY → `investment_buy`, SELL → `investment_sell`, CONT → `investment_contribution`, FEE → `investment_fee`, GRANT → `investment_grant`, REIMB → `investment_rebate`.

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

## Credit card strategy

**Points harvest, pay-in-full discipline.** Big household purchases go on CC to earn points; the goal is to pay the full balance before the due date — no revolving balance, no interest. The CFO's job is to give AJ enough forward visibility to anticipate each payment, plan timing around income deposits, and never get caught carrying a balance by accident.

Statement close dates: **TD Visa closes ~20th, MBNA closes ~17th — AJ treats both as the 20th** for planning purposes. Payment due is typically ~21–25 days after close (~15th of the following month). Natural payment window: Polina's EoM deposit or AJ's first Friday of the month.

## Constants

- `MORTGAGE_BUFFER_GOAL = 2140` CAD — one Desjardins mortgage payment; the target chequing floor
- **Buffer account:** the $2,140 floor should be held in **TD Chequing** (not Desjardins). Goal is to build it there over time.
- **Desjardins PCA exclusion:** the Desjardins PCA balance is Desjardins credit union dividends, earmarked for the hot water tank replacement. It must **not** be counted toward the liquid floor or general cash position — exclude it from `finance liquid` chequing totals and buffer gap math.

## Gate to live sync

Wealthica API credentials must be received from `hello@wealthica.com` and stored in `~/.jarvis.env` before first live sync. Mock mode works immediately for development.

## Testing

```bash
python3 -m pytest tests/test_finance.py -v
```

No live API calls. 41 unit tests covering DB, CSV import, recurring detection, alerts, brief, and all P1 commands.
