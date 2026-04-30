# Project Jarvis

A personal AI assistant built on Claude, designed to run 24/7 on a home server and act as an intelligent layer over real life — email, calendar, home automation, garden, and more. Controlled entirely through Discord.

This is an active portfolio project. Logic is public; personal data stays local.

---

## What it does today

- **Triages Gmail** using Claude Haiku as a classifier. Sender rules are cached in SQLite — repeat senders never hit the API. New senders are classified and staged to Discord for approval before anything is touched. Supports watch rules (semantic alerts on named criteria), archive expiry (per-tag retention policies), real HTTP unsubscribes (RFC 8058 one-click POST + GET fallback), and a flag-and-remove flow for emails Haiku can't confidently classify. Nothing executes without an explicit confirm.

- **Reads and writes Google Calendar** across personal and shared family calendars. Returns formatted daily/weekly views to Discord. Feeds upcoming events into the Gmail classifier so appointment confirmation emails are archived automatically.

- **Runs a heartbeat** every 10 minutes during waking hours. Stays silent unless there's something actionable — priority emails, watch rule matches, or uncertain emails needing a decision. Non-priority emails accumulate in a digest queue posted at 13:00 and 18:00.

- **Posts a daily briefing at 7am** via cron: weather, today's calendar, overnight priority inbox, BBC headlines, and personalized news by topic. Single Sonnet call, always posted verbatim to Discord.

---

## Design philosophy

**Stage then approve.** The agent never takes an irreversible action without a human checkpoint. Propose, present, wait. This applies to email deletion, event creation, anything with consequences. Enforced at the skill level — it can't be bypassed by a confused agent.

**Graceful degradation.** Never fail silently. If a skill can't run, surface the problem. If a classification is uncertain, flag it for the user rather than guess. Always leave the user better off than before the run.

**Single surface.** Everything flows through Discord. No separate apps, no dashboards to check. One place, one context.

**Security by design.** Secrets never touch the repo. Personal data never leaves the local layer. The public codebase exposes logic, never state. OAuth tokens, SQLite databases, and personal config are all gitignored.

**Cost discipline.** Claude Haiku for classification and rule-based decisions. Claude Sonnet for reasoning and drafting. Never Opus for automated tasks. Target: $5–15/month at steady state.

---

## Architecture

```
┌─────────────────────────────────────┐
│  Discord (single control surface)   │
└──────────────────┬──────────────────┘
                   │
┌──────────────────▼──────────────────┐
│  OpenClaw (agent runtime)           │
│  Claude Sonnet · workspace context  │
│  Discord gateway                    │
└──────────────────┬──────────────────┘
                   │ bash
┌──────────────────▼──────────────────┐
│  Python skills (one venv each)      │
│  gmail-cleanup · calendar · ...     │
└──────────────────┬──────────────────┘
                   │
┌──────────────────▼──────────────────┐
│  SQLite  ·  Gmail API  ·  GCal API  │
│  Home Assistant  ·  Spotify  · ...  │
└─────────────────────────────────────┘
```

Scheduled tasks (heartbeat, digest, briefing) run via WSL cron and post to Discord directly via webhook — they do not go through OpenClaw.

### Infrastructure layers

| Layer | Where | What runs |
|---|---|---|
| Local brain | Windows PC / WSL2 | OpenClaw, Python skills, SQLite, cron |
| Cloud brain | Hetzner VPS + Cloudflare tunnel *(Phase 4)* | Discord bot, job scraper |
| Hardware controller | Raspberry Pi 5 *(Phase 4)* | Home Assistant, Zigbee2MQTT, irrigation DAGs |

### Key decisions

- **SQLite as shared memory.** Skills query on demand. Never preloaded into Claude context.
- **Python for all skill logic.** One virtualenv per skill. OpenClaw shells out via bash.
- **Skills are isolated.** Cross-skill communication happens via subprocess JSON, not imports.
- **Zigbee over WiFi for lights.** Sonoff USB dongle. Avoids network congestion at scale.
- **Home Assistant as the smart home API.** One skill controls all devices. Never bypass HA.

---

## Phases

| Phase | Status | Goal |
|---|---|---|
| Phase 1 | ✅ Done | Foundation — OpenClaw, Discord bot, dev environment |
| Phase 2 | 🔄 In progress | Core integrations — Gmail, Calendar, Home Assistant, Spotify |
| Phase 3 | 🔄 In progress | Agentic skills — morning briefing ✅, job search, garden planning |
| Phase 4 | 🔲 Planned | Infrastructure — VPS migration, Raspberry Pi deployment, Tailscale mesh |
| Phase 5 | 🔲 Planned | Jarvis Android app (Flutter, sideloaded APK) |

---

## Skills

| Skill | Status | Description |
|---|---|---|
| `gmail-cleanup` | ✅ Live | Haiku classifier, SQLite rule cache, stage/execute/adjust, watch rules, archive expiry, flag-and-remove, real unsubscribe |
| `calendar` | ✅ Live | Google Calendar read/write, multi-calendar, Discord formatting, JSON output for integrations |
| `morning-briefing` | ✅ Live | Daily 7am briefing: weather, calendar, priority inbox, BBC headlines, personalized news |
| `home-assistant` | 🔲 Planned | Control lights, fan, cameras via HA REST API |
| `spotify` | 🔲 Planned | Playback control, queue management |
| `job-search` | 🔲 Planned | Scrape listings, deduplicate via SQLite, surface matches |
| `garden` | 🔲 Planned | Hardiness zone 4-5 planting calendar, irrigation scheduling via Pi GPIO |
| `notes` | 🔲 Planned | Quick capture to SQLite, searchable via Discord |
| `tasks` | 🔲 Planned | Task tracking with Discord interface |
| `pc-control` | 🔲 Planned | Shutdown, sleep, app control via WSL |

---

## Stack

| Component | Technology |
|---|---|
| Agent runtime | [OpenClaw](https://openclaw.ai) |
| AI models | Claude Haiku (classification) · Claude Sonnet (reasoning) |
| Skill language | Python 3.12 |
| Persistent memory | SQLite |
| Control surface | Discord |
| Scheduled tasks | WSL cron → Discord webhook |
| Smart home | Home Assistant + Zigbee2MQTT *(Phase 4)* |
| CI/CD | GitHub Actions — security scan, ruff + pytest, docker validation |

---

## CI/CD

Three workflows gate every merge to `main`:

| Workflow | Tools |
|---|---|
| Security scan | TruffleHog (full history) + Gitleaks |
| Python quality | Ruff lint + format · pytest (Python 3.11 & 3.12) |
| Docker validation | `docker compose config` with `.env.example` |

---

## Running locally

```bash
# Clone and set up dev environment
git clone https://github.com/ajpoole1/project-jarvis.git
cd project-jarvis
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-dev.txt

# Set up a skill
cd skills/gmail-cleanup
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt

# Copy and fill in env vars
cp .env.example .env
```

See each skill's `README.md` for credential setup and first-run instructions.

---

## Repository structure

```
jarvis/
├── scripts/               # Cron scripts — heartbeat, digest, briefing
├── skills/
│   ├── gmail-cleanup/     # Gmail classifier and triage
│   ├── calendar/          # Google Calendar read/write
│   └── morning-briefing/  # Daily 7am Discord briefing
├── config/
│   ├── personal/          # gitignored — OAuth tokens, personal config
│   └── examples/          # committed — fake data showing structure
├── data/                  # gitignored — SQLite DB
├── logs/                  # gitignored
└── .github/workflows/     # CI/CD pipelines
```
