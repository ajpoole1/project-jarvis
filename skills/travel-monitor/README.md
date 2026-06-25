# travel-monitor

Flexible-date travel-cost tracker. For a given destination + date window, scans three
independent lanes — flights (Amadeus), hotels by-provider (Firecrawl), vacation packages
(Firecrawl) — reconciles package vs. DIY (flight + hotel), and alerts on price drops via
Discord. Alerts only; never books.

Mirrors `price-monitor` in structure. The key divergence: a watch is date-windowed and
multi-lane, not `one URL → one price`.

## What it does

- **Flights lane** — Amadeus Flight Cheapest Date Search (triage) + Offers Search (confirm).
  One OAuth2 call per run, one search call per watch. Returns date→price across the window.
- **Hotels lane** — Firecrawl scrape of the named property's by-provider + date-calendar view.
  One scrape per watch per run; the calendar contains the whole window server-side.
- **Packages lane** — Firecrawl scrape of each enabled operator's flex-date search.
  Registered operators: costco, aircanada, westjet, expedia. Wire one at a time in Phase 3.
- **Reconciliation** — joins flight + hotel on matching check-in dates → best DIY cost.
  Compares against best package. Posts the winning option + delta.
- **Deal detection** — target price hit / ≥N% below trailing median / N-day low.
  Silent run unless a deal or health alert fires.
- **Health alerts** — each lane fails independently; loud Discord alert + `unavailable` mark.
  Succeeded lanes still report. No silent drops.

## Commands

```
python3 skill.py add  --name "Punta Cana Feb" --dest PUJ --origin YUL \
    --window-start 2026-02-01 --window-end 2026-02-21 --nights 6 \
    --adults 2 [--children 0] [--hotel "Hard Rock Punta Cana"]... \
    [--operator costco] [--operator aircanada] \
    [--target-price 2900] [--cadence weekly]

python3 skill.py list

python3 skill.py resolve --id 3 --action booked|passed

python3 skill.py check          # scheduled run; silent unless deal/health
```

`add` probes each enabled lane once at add-time (readability check). Any lane that fails is
surfaced, not hidden. If all lanes fail, offers a `follow_ups` reminder fallback.

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `AMADEUS_CLIENT_ID` | — | Amadeus Self-Service API client ID (production) |
| `AMADEUS_CLIENT_SECRET` | — | Amadeus Self-Service API client secret (production) |
| `FIRECRAWL_API_KEY` | — | Firecrawl API key; required for hotels + packages lanes |
| `JARVIS_DATA_DIR` | `/data` | SQLite DB location |
| `TRAVEL_MONITOR_DEAL_DROP_PCT` | `10` | % below trailing median to flag a deal |
| `TRAVEL_MONITOR_N_MEDIAN` | `8` | Prior readings for median calculation |
| `TRAVEL_MONITOR_N_DAY_LOW` | `30` | Window (days) for N-day-low detection |
| `TRAVEL_MONITOR_ESCALATE_DAYS` | `21` | Days before window_start to flip cadence weekly→daily |
| `DISCORD_WEBHOOK_URL` | — | Required for deal/health notifications |

## Package operators

Registered: `costco`, `aircanada`, `westjet`, `expedia`. Each requires its own Firecrawl
scraper (Phase 3). Add `--operator <name>` per watch; multiple flags allowed.

## DB tables (jarvis.db)

- `travel_watches` — one row per watch; tracks destination, window, lane config, cadence
- `travel_price_history` — append-only per-lane readings; drives median and N-day-low

## Firecrawl credits

Hotels lane: ~5–9 credits/scrape (stealth + JSON-extract on bot-protected pages).
Packages lane: ~5–9 credits/scrape per operator. Budget: 180–320 credits/month at 3 watches
weekly; 1.3–2.2k/month at daily-escalation. Inside Hobby tier.

## Amadeus

Uses production environment (test is cached/limited). OAuth2 client-credentials; token
fetched once per run (TTL ~30 min). Flight Cheapest Date Search is cached data (free triage);
Offers Search is live (confirms price + carrier). Free monthly quota: 200–10k req/mo.

## Scheduling

Register via the `schedules` dispatcher. Default cadence: weekly. Escalates to daily when
`window_start` is within `TRAVEL_MONITOR_ESCALATE_DAYS` days (default 21).

```
python3 skills/schedules/skill.py propose \
    --skill travel-monitor \
    --schedule weekly@09:00 \
    --description "Weekly travel price check" \
    --args '["check"]'
```

**Run location:** home WSL2 box only (residential IP). Datacenter IPs get blocked by
hotel/package sites far more aggressively than retail.

## Phasing

- **Phase 0** ✅ — Scaffold: skill dir, schema, command-surface stub, env wiring
- **Phase 1** ✅ — Flights lane: Amadeus OAuth + Cheapest Date Search + Offers Search
- **Phase 2** ✅ — Hotels lane: Firecrawl JSON-extract, Google Hotels, named property price calendar
- **Phase 3** — Packages lane: Firecrawl per operator (wire one at a time)
- **Phase 4** — Reconciliation + deal detection + Discord alert
- **Phase 5** — Health/degradation hardening + cadence escalation

## Notes

- Alerts only — the skill never books anything
- v1 scope: flexible dates, fixed named hotel(s). Flexible hotel selection deferred to v2.
- Charter carriers (Transat, Sunwing) out of scope; Amadeus GDS doesn't carry them.
- Skill code is read-only to the agent (write boundary in settings.json)
