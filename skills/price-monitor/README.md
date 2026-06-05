# price-monitor

Watches product URLs for price drops using free structured data. No paid API, no virtualenv.

## What it does

- **Tracks prices** on Shopify stores, JSON-LD retailers, and Open Graph sites
- **Alerts on drops** via Discord when price hits a target, drops ≥15% from median, or hits an N-day low
- **Degrades gracefully** — URLs that can't be read are offered as `follow_ups` reminders instead of silent dead watches
- **Runs daily** via the `schedules` dispatcher (Brief 5)

## Parse cascade (in order)

1. **Shopify JSON** — `<product-url>.json` endpoint; clean, free, covers most DTC stores
2. **JSON-LD** — `<script type="application/ld+json">` schema.org Product/Offer nodes
3. **Open Graph / itemprop** — `product:price:amount`, `og:price:amount`, `itemprop="price"` meta tags
4. **Regex** — currency pattern near a "price" keyword (low confidence, last resort)

**Honest expectation:** Amazon, Walmart, and aggressively bot-blocked sites will almost always fall back to reminders. Shopify and JSON-LD sites track reliably.

## Commands

```
python3 skill.py add --name "Item name" --url "https://..." \
    [--target-price 49.99] [--active-from 2026-06-01] [--active-until 2026-12-31]

python3 skill.py list

python3 skill.py resolve --id 3 --action bought|passed

python3 skill.py check          # scheduled daily run
```

### `add` output

- **Readable URL** → creates an active watch, returns JSON with detected price and method
- **Unreadable URL** → returns `{"status": "needs_reminder", ...}` — agent offers a `follow_ups` reminder instead

## Env vars (all optional)

| Var | Default | Purpose |
|---|---|---|
| `JARVIS_DATA_DIR` | `/data` | SQLite DB location |
| `PRICE_MONITOR_FAIL_THRESHOLD` | `3` | Consecutive failures before degrading to a reminder |
| `PRICE_MONITOR_DEAL_DROP_PCT` | `15` | % below trailing median to flag a deal |
| `PRICE_MONITOR_N_MEDIAN` | `10` | Number of prior readings for median calculation |
| `PRICE_MONITOR_N_DAY_LOW` | `30` | Window (days) for N-day-low detection |
| `DISCORD_WEBHOOK_URL` | — | Required for deal/degrade notifications |

## Scheduling

Propose the daily run via `schedules`:

```
python3 skills/schedules/skill.py propose \
    --skill price-monitor \
    --schedule daily@08:30 \
    --description "Daily price check" \
    --args '["check"]'
```

Then approve with `schedules approve <id>`. The run is silent unless a deal or degrade fires.

## DB tables

- `price_watches` — one row per watch; tracks URL, last price, method, status, fail count
- `price_history` — append-only price readings; used for median and N-day-low deal detection

Runs from the home WSL2 box (residential IP) — datacenter IPs get blocked far more often.

## Notes

- Alerts only — the skill never purchases anything
- One GET per watch per run; no retries, no proxies
- Skill code is read-only to the agent (write boundary enforced in `settings.json`)
