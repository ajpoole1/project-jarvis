# Project Jarvis — Claude Code Memory

Personal AI assistant. Stack: OpenClaw (Node.js) · Claude API · Python · Docker · Discord · Home Assistant.
This is a portfolio-grade open source project. Logic is public, personal data is always local-only.

---

## Core Philosophies — These govern every design decision

- **Stage then approve** — The agent NEVER takes irreversible action without a human checkpoint. Propose, present, wait for confirmation. Applies to Gmail deletion, purchases, job applications, anything with consequences. This is non-negotiable.
- **Graceful degradation** — Never fail silently. Always leave the user better off: a pre-filled link, a drafted message, a summarized situation. Reduce friction, don't demand perfection.
- **Single surface, unified context** — Everything flows through Discord (now), Jarvis app (Phase 5). Skills share context lazily via RAG — loaded on demand, never preloaded into every call.
- **Security by design** — Secrets never touch the repo. Personal data never leaves the local layer. The public codebase exposes logic, never state.

---

## Architecture

### Infrastructure layers

| Layer | Where | What runs |
|---|---|---|
| Local brain | Windows PC / WSL2 | VS Code, Docker Compose, OpenClaw, Python skills, SQLite |
| Cloud brain | Hetzner VPS + Cloudflare tunnel | Discord bot, job scraper, Gmail agent, morning briefing (Phase 4) |
| Hardware controller | Raspberry Pi 5 8GB | Home Assistant Container, Zigbee2MQTT, Airflow irrigation DAGs (Phase 4+) |

### Key design decisions — Do not revisit without good reason

- **SQLite is the shared memory layer.** Skills query it on demand. It is never preloaded into Claude context.
- **Python for all skill logic.** One virtualenv per skill. OpenClaw shells out to Python.
- **Zigbee over WiFi for lights.** Sonoff USB dongle on Pi. Avoids network congestion at scale.
- **Home Assistant is the smart home API.** One skill controls all devices. Never bypass HA to talk to devices directly.
- **VPS + Pi hybrid.** Cloud for resilience, local for hardware control. They connect via Tailscale.

---

## Skill Conventions — Follow these exactly

Every skill lives in `/skills/<skill-name>/`:
- `skill.py` — entry point, called by OpenClaw
- `requirements.txt` — dependencies for this skill's virtualenv
- `README.md` — what it does, what env vars it needs, how to test it

**Model selection:**
- Use `claude-haiku-*` for classification, tagging, rule-based decisions
- Use `claude-sonnet-*` for reasoning, drafting, semantic matching
- Never use Opus for automated/scheduled tasks — cost is not justified

**SQLite usage:**
- DB file lives in `/data/jarvis.db` — this path is gitignored
- Skills read/write via the shared schema — never create skill-specific DBs
- Rule caches (Gmail sender rules, job deduplication) go in SQLite, not in memory

**Logging:**
- Logs show actions taken, never content processed
- No PII in logs — redact email subjects, names, message bodies
- Log to `/logs/<skill-name>.log` — gitignored

---

## Security Rules — Hard rules, never break these

- **Never hardcode secrets.** All credentials via `.env` (local) or environment variables (VPS).
- **Never commit:** `.env`, `/data/`, `/logs/`, `/config/personal/`, any SQLite `.db` file, Home Assistant config
- **Always commit:** `.env.example` with placeholder values, `/config/examples/` with fake data
- **Payment skill:** card token reference only — raw card number never stored, logged, or passed as a string
- **Gmail OAuth2:** request minimum scopes per skill — never request broad access
- **Before any PR to main:** check that no personal data files have been staged

---

## Repo Structure

```
jarvis/
├── CLAUDE.md                  ← you are here
├── CONTEXT.md                 ← update at end of each session
├── .env.example               ← committed, placeholder values only
├── docker-compose.yml
├── pyproject.toml             ← Ruff config
├── requirements-dev.txt       ← ruff, pytest, pytest-cov
├── skills/
│   ├── gmail-cleanup/
│   ├── job-search/
│   ├── morning-briefing/
│   ├── home-assistant/
│   ├── garden/
│   ├── airflow-monitor/
│   ├── calendar/
│   ├── tasks/
│   ├── notes/
│   ├── browser-automation/
│   ├── pc-control/
│   └── spotify/
├── config/
│   ├── personal/              ← gitignored, all personal config here
│   └── examples/              ← committed, fake data showing structure
├── data/                      ← gitignored, SQLite DB lives here
├── logs/                      ← gitignored
└── .github/
    └── workflows/
        ├── security.yml       ← TruffleHog + gitleaks
        ├── quality.yml        ← Ruff + pytest
        └── docker.yml         ← compose validation
```

---

## Phase Status

| Phase | Status | Goal |
|---|---|---|
| Phase 1 | 🔲 Not started | Foundation — OpenClaw, Discord, first voice note |
| Phase 2 | 🔲 Not started | Core integrations — HA, Gmail, Calendar, Spotify |
| Phase 3 | 🔲 Not started | Agentic skills — Gmail cleanup, job search, morning briefing, garden |
| Phase 4 | 🔲 Not started | VPS migration + Pi deployment |
| Phase 5 | 🔲 Not started | Jarvis Android app (Flutter, sideloaded APK) |

Update this table as phases complete. Use: 🔲 Not started / 🔄 In progress / ✅ Done

---

## CI/CD Pipeline

Three GitHub Actions workflows — all must pass before merging to `main`:

| Workflow | File | Tools |
|---|---|---|
| Security scan | `security.yml` | TruffleHog (full history) + gitleaks |
| Python quality | `quality.yml` | Ruff + pytest (Python 3.11 + 3.12 matrix) |
| Docker validation | `docker.yml` | `docker compose config` with `.env.example` |

Branch strategy: `feature/skill-name` → `develop` → `main` (protected)

---

## Smart Home Reference

| Device | Protocol | Integration | Phase |
|---|---|---|---|
| Lorex cameras | RTSP | HA direct stream | Phase 2 |
| Smart Life fan | Tuya | HA Tuya integration | Phase 2 |
| Ceiling pot lights | Zigbee BR30 | Zigbee2MQTT → HA | Phase 5 |
| Kitchen wired fixtures | — | Smart dimmer or WiZ retrofit | Phase 5 |
| Google Home devices | — | Managed via HA | Phase 2 |
| Irrigation solenoids | GPIO | HA + Pi GPIO | Future |

---

## Garden — Special Rules

- Quebec hardiness zone 4-5
- Wife's garden section is **read-only**. Jarvis acknowledges it exists, never acts on it without an explicit direct request naming it.
- Primocane vs floricane raspberries have different pruning rules — always check variety before any garden action.

---

## Cost Targets

- Steady state API cost: $5–15 USD/month
- Use Haiku for classification to stay in this range
- Morning briefing = one batched Sonnet call per day (most predictable cost)

---

## Session Handoff

At the end of each work session, update `CONTEXT.md` with:
1. What was completed (be specific: file paths, function names)
2. What is in progress and where it was left
3. What is blocked and why
4. What is next

Do not skip this step. It is how context survives between sessions.
