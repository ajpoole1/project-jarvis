# Project Jarvis — Claude Code Memory

Personal AI assistant. Stack: OpenClaw (Node.js) · Claude API · Python · Docker · Discord · Home Assistant.
Private repo. Personal data and knowledge live here. Logic and personal state are both local-only.

---

## Core Philosophies — These govern every design decision

- **Stage then approve** — The agent NEVER takes irreversible action without a human checkpoint. Propose, present, wait for confirmation. Applies to Gmail deletion, purchases, job applications, anything with consequences. This is non-negotiable.
- **Graceful degradation** — Never fail silently. Always leave the user better off: a pre-filled link, a drafted message, a summarized situation. Reduce friction, don't demand perfection.
- **Single surface, unified context** — Everything flows through Discord (now), Jarvis app (Phase 5). Skills share context lazily via RAG — loaded on demand, never preloaded into every call.
- **Security by design** — Secrets never touch the repo. Personal data never leaves the local layer. The agent executes skills; it does not author them. Push is always a human action.

---

## Architecture

### Infrastructure layers

| Layer | Where | What runs |
|---|---|---|
| Local brain | Windows PC / WSL2 | VS Code, Docker Compose, OpenClaw, Python skills, SQLite |
| Cloud brain | Hetzner VPS + Cloudflare tunnel | Discord bot, job scraper, Gmail agent, morning briefing (Phase 4) |
| Hardware controller | Windows PC / WSL2 | Home Assistant Container (Phase 2+). Raspberry Pi + garden irrigation deferred to 2027. |

### Key design decisions — Do not revisit without good reason

- **SQLite is the shared memory layer.** Skills query it on demand. It is never preloaded into Claude context.
- **`knowledge/` is the personal knowledge base.** Markdown files committed to the private repo. Agent reads on demand (lazy RAG), writes only on explicit AJ request. Schema and structure documented in `knowledge/KNOWLEDGE.md`. Never preloaded — always searched. FTS5 index (`skills/knowledge/`) rebuilt from Markdown on each call; no embeddings; no ingest-pdf (one-off PDF work handled in code, not a skill command).
- **Python for all skill logic.** One virtualenv per skill. OpenClaw shells out to Python.
- **Home Assistant is the smart home API.** One skill controls all devices. Never bypass HA to talk to devices directly. HA runs natively in WSL2 (Python venv at `/srv/homeassistant`), not in Docker.
- **VPS + Pi hybrid is Phase 4+.** Current setup is PC-only. Zigbee, Pi, and garden irrigation are 2027 scope.

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

- **Never hardcode secrets.** All credentials via `~/.jarvis.env` (local, 600 permissions, WSL2 home) or environment variables (VPS). Never put secrets in `/mnt/c/` paths — DrvFs does not enforce file permissions.
- **Never commit:** `.env`, `~/.jarvis.env`, `/data/`, `/logs/`, `/config/personal/`, any SQLite `.db` file, Home Assistant config
- **Always commit:** `.env.example` with placeholder values, `/config/examples/` with fake data
- **Payment skill:** card token reference only — raw card number never stored, logged, or passed as a string
- **Gmail OAuth2:** request minimum scopes per skill — never request broad access. Gmail skill: read + modify (no send). Calendar: read + write events. Never request full account access.
- **Before any PR to main:** check that no personal data files have been staged
- **Shell is allowlisted.** The agent's shell access is restricted via `~/.openclaw/workspace/.claude/settings.json`. Egress commands (`git push`, `curl`, `wget`, `ssh`), escape hatches (`bash -c`, `python -c`, `eval`), and destructive ops (`rm -rf`) are hard-denied. Do not expand the allowlist without understanding the injection chain risk.
- **Skills and scripts are read-only to the agent.** Edit and Write tools are denied for `skills/` and `scripts/` in the agent's settings. You write skills (from Claude Code or your IDE); the agent runs them. Never grant the agent write access to skill files.
- **No autonomous push.** The agent may branch and commit locally. `git push` is hard-denied at the tool level and listed as a Red Line in AGENTS.md. Push is always initiated by you.
- **Injection guard is in SOUL.md.** Treat it as the weakest layer — the structural controls above are what actually hold. Do not rely on the instruction layer alone.

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
| Phase 1 | ✅ Done | Foundation — OpenClaw, Discord, first voice note |
| Phase 2 | ✅ Done | Core integrations — HA (fan + LocalTuya LAN + Lorex cameras), Gmail, Calendar. Google Home + Spotify deferred. |
| Phase 3 | 🔄 In progress | Agentic skills — Gmail cleanup ✅, morning briefing ✅, garden ✅, tasks ✅, knowledge skill (FTS5) next. Job search deferred. |
| Phase 4 | 🔲 Deferred | VPS migration + Pi deployment — deferred until Android app is ready and security is properly tested. Jarvis stays local-only until then. |
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

| Device | Protocol | Integration | Status |
|---|---|---|---|
| Smart Life fan (XFBD410) | LocalTuya LAN (protocol 3.5) | `switch.ellie_s_register_fan_power`, mode, speed, temp, child lock | ✅ Phase 2 done |
| Lorex cameras (4 of 5) | RTSP → go2rtc → HA generic | `camera.front_yard`, `driveway`, `back_garage`, `pool` — WebRTC live feed | ✅ Phase 2 done |
| Lorex doorbell | RTSP → go2rtc | `camera.doorbell` — blocked, unknown device password | 🔄 Phase 2 pending |
| Google Home devices | Google Cast | HA Google Cast integration | 🔲 Phase 2 pending |
| Ceiling pot lights | Zigbee BR30 | Zigbee2MQTT → HA | 🔲 Phase 5 |
| Kitchen wired fixtures | — | Smart dimmer or WiZ retrofit | 🔲 Phase 5 |
| Irrigation solenoids | GPIO | HA + Pi GPIO | 🔲 2027+ |

### HA architecture

HA Core 2025.1.4 runs as a systemd service in WSL2 (`/srv/homeassistant` venv, Python 3.12). Config at `/home/ajpoole/.homeassistant/`. WSL2 mirrored networking (`~/.wslconfig`) gives HA direct LAN access — required for LocalTuya UDP discovery.

**Fan:** LocalTuya 2025.11.0 (xZetsubou fork) over protocol 3.5. Full local control — mode, speed, temp, child lock, display.

**Cameras:** go2rtc (`docker-compose.yml`) proxies RTSP streams from all 4 working Lorex cameras. HA generic camera entities consume go2rtc WebRTC streams. Doorbell (`192.168.2.40`) is excluded — device password unknown. Fix: factory reset the doorbell, re-pair in Lorex app, update `config/personal/go2rtc.yaml`.

**go2rtc config** lives in `config/personal/go2rtc.yaml` (gitignored). Contains camera RTSP URLs with credentials — never commit.

---

## Garden — Special Rules

- Quebec hardiness zone 4-5
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
