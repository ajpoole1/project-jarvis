# Project Jarvis

A personal AI assistant built on Claude, designed to run 24/7 on a home server and act as an intelligent layer over real life — email, calendar, home automation, garden, and more. Controlled entirely through Discord.

This is an active portfolio project. Logic is public; personal data stays local.

---

## What it does today

- **Reads and triages Gmail** using Claude Haiku as a classifier. Sender rules are cached in SQLite so repeat senders never hit the API. New senders are classified, staged, and surfaced to Discord for approval before anything is touched. Nothing executes without an explicit confirm.
- **Reads and writes Google Calendar** across personal and shared family calendars. Returns formatted daily/weekly views to Discord. Feeds upcoming events into the Gmail classifier so appointment confirmation emails are archived automatically.
- **Runs on a heartbeat** — checks Gmail 3× per day during waking hours and only reaches out if there's something actionable. Stays silent otherwise.

---

## Design philosophy

**Stage then approve.** The agent never takes an irreversible action without a human checkpoint. Propose, present, wait. This applies to email deletion, event creation, anything with consequences. Enforced at the skill level — it can't be bypassed by a confused agent.

**Graceful degradation.** Never fail silently. If a skill can't run, surface the problem. If a classification is uncertain, flag it rather than guess. Always leave the user better off than before the run.

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
│  Heartbeat poller · Discord gateway │
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

### Infrastructure layers

| Layer | Where | What runs |
|---|---|---|
| Local brain | Windows PC / WSL2 | OpenClaw, Python skills, SQLite |
| Cloud brain | Hetzner VPS + Cloudflare tunnel *(Phase 4)* | Discord bot, job scraper, morning briefing |
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
| Phase 3 | 🔲 Planned | Agentic skills — morning briefing, job search, garden planning |
| Phase 4 | 🔲 Planned | Infrastructure — VPS migration, Raspberry Pi deployment, Tailscale mesh |
| Phase 5 | 🔲 Planned | Jarvis Android app (Flutter, sideloaded APK) |

---

## Skills

| Skill | Status | Description |
|---|---|---|
| `gmail-cleanup` | ✅ Live | Haiku classifier, SQLite rule cache, stage/execute/adjust flow, Calendar integration |
| `calendar` | ✅ Live | Google Calendar read/write, multi-calendar, Discord formatting, JSON output for integrations |
| `morning-briefing` | 🔲 Planned | Daily 9am Sonnet call: calendar events, inbox flags, overnight activity |
| `home-assistant` | 🔲 Planned | Control lights, fan, cameras via HA REST API |
| `spotify` | 🔲 Planned | Playback control, queue management |
| `job-search` | 🔲 Planned | Scrape listings, deduplicate via SQLite, surface matches |
| `garden` | 🔲 Planned | Hardiness zone 4-5 planting calendar, irrigation scheduling via Pi GPIO |
| `notes` | 🔲 Planned | Quick capture to SQLite, searchable via Discord |
| `tasks` | 🔲 Planned | Task tracking with Discord interface |
| `browser-automation` | 🔲 Planned | Form filling, login flows, scraping |
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
| Smart home | Home Assistant + Zigbee2MQTT |
| Automation | Apache Airflow *(Phase 4)* |
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
├── skills/
│   ├── gmail-cleanup/     # Gmail classifier and triage
│   └── calendar/          # Google Calendar read/write
├── config/
│   ├── personal/          # gitignored — OAuth tokens, personal config
│   └── examples/          # committed — fake data showing structure
├── data/                  # gitignored — SQLite DB
├── logs/                  # gitignored
└── .github/workflows/     # CI/CD pipelines
```
