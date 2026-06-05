# price-monitor

Watches product URLs for price drops. Fetch layer uses Firecrawl to bypass anti-bot / JS rendering; parse cascade extracts structured price data from the returned HTML.

## What it does

- **Tracks prices** via Firecrawl-rendered HTML + structured data cascade
- **Alerts on drops** via Discord when price hits a target, drops ≥15% from median, or hits an N-day low
- **Degrades gracefully** — URLs that can't be read are offered as `follow_ups` reminders instead of silent dead watches
- **Runs daily** via the `schedules` dispatcher

## Fetch + parse cascade (in order)

1. **Shopify JSON** — `<product-url>.json` endpoint via direct urllib; clean, free, covers most DTC stores
2. **Firecrawl rawHtml scrape** — POST to Firecrawl API; returns rendered HTML, bypassing anti-bot and JS-rendered pages (1 credit). Falls back to direct urllib GET when `FIRECRAWL_API_KEY` is absent.
3. **JSON-LD** — `<script type="application/ld+json">` schema.org Product/Offer nodes from the HTML
4. **Open Graph / itemprop** — `product:price:amount`, `og:price:amount`, `itemprop="price"` meta tags
5. **Regex** — currency pattern near a "price" keyword (low confidence, last resort)
6. **Firecrawl LLM extraction** — only when steps 3–5 all fail; Firecrawl's schema-based LLM extraction (extra credits). Result stored as `method=firecrawl_extract`.
7. **Degrade to reminder** — if nothing works, offer a `follow_ups` weekly reminder instead of a dead watch

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

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `FIRECRAWL_API_KEY` | — | Firecrawl API key; enables Firecrawl fetch + LLM extraction. Without it, falls back to direct urllib (lower hit rate). |
| `JARVIS_DATA_DIR` | `/data` | SQLite DB location |
| `PRICE_MONITOR_FAIL_THRESHOLD` | `3` | Consecutive failures before degrading to a reminder |
| `PRICE_MONITOR_DEAL_DROP_PCT` | `15` | % below trailing median to flag a deal |
| `PRICE_MONITOR_N_MEDIAN` | `10` | Number of prior readings for median calculation |
| `PRICE_MONITOR_N_DAY_LOW` | `30` | Window (days) for N-day-low detection |
| `DISCORD_WEBHOOK_URL` | — | Required for deal/degrade notifications |

## Firecrawl credits

- Free tier: ~1,000 credits/month (verify at firecrawl.dev — no rollover)
- 1 credit per base rawHtml scrape; LLM extraction costs more (fallback only)
- Quota exhaustion (402) → degrade-to-reminder, no crash, no silent miss
- Credit usage logged to stderr when `creditsUsed` is returned in metadata

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

**Run location:** home WSL2 box only (residential IP). Datacenter IPs get blocked far more aggressively by retail sites.

## DB tables

- `price_watches` — one row per watch; tracks URL, last price, method, status, fail count
- `price_history` — append-only price readings; used for median and N-day-low deal detection

## Notes

- Alerts only — the skill never purchases anything
- One Firecrawl scrape per watch per run; no retries
- Skill code is read-only to the agent (write boundary enforced in `settings.json`)
