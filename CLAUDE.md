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
- **Two-root knowledge model.** `knowledge/` (committed) holds generic reference content (garden, recipes, preferences, projects). `~/.jarvis/knowledge/` (private, local-only, 700/600 perms, WSL2 home ext4) holds personal/people/home content. Agent reads both on demand (lazy RAG); writes route via the knowledge skill using `TIERS.md` domain mapping. See `knowledge/KNOWLEDGE.md` and `knowledge/TIERS.md` for full schema. FTS5 index rebuilt on each search call; no embeddings; no ingest-pdf. Both roots are agent-writable for `*.md` only — see Write Boundary section below.
- **Python for all skill logic.** One virtualenv per skill for skills with third-party dependencies. Stdlib-only skills (`followups`, `schedules`, `knowledge`, `price-monitor`) are intentionally venv-free — their `requirements.txt` documents this explicitly. The dispatcher auto-selects `skills/<name>/.venv/bin/python` if present, falling back to system `python3` for stdlib-only skills. OpenClaw shells out to Python.
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
- **Exception:** suites that are large, sensitive, or multi-table domain systems (e.g. `finance/`) may use a dedicated DB file (e.g. `/data/finance.db`). This must be an explicit architectural decision, not a lazy default. The rule for routine skills remains `jarvis.db`.
- Rule caches (Gmail sender rules, job deduplication) go in SQLite, not in memory

**Logging:**
- Logs show actions taken, never content processed
- No PII in logs — redact email subjects, names, message bodies
- Log to `/logs/<skill-name>.log` — gitignored

**Scheduling:**
- **One cron surface — single line.** The crontab has exactly one job: `*/10 7-23 * * *  cron_followups.sh`. Everything runs through the `schedules` skill dispatcher. Jarvis proposes jobs (`schedules propose`); AJ approves; the heartbeat dispatches them. Jarvis never writes crontab. Never create ad-hoc crons for user tasks.
- **Current approved schedules (in `jarvis.db`):** gmail-cleanup heartbeat (10m), gmail-cleanup digest (daily@13:00 + daily@18:00), morning-briefing (daily@07:00). All dispatched by the single heartbeat tick.
- **Dispatcher Discord routing:** the dispatcher forwards any non-empty skill stdout to Discord. Skills that need multi-part posting (e.g. morning-briefing with its 3-message split) post to Discord themselves via a `_post_discord()` subprocess helper — they produce no stdout. Error alerts (non-zero exit, skill-not-found, timeout) are always posted by the dispatcher regardless.
- **Heartbeat liveness:** the dispatcher writes `last_tick` to a `heartbeat` SQLite table on every tick and posts a Discord alert if the gap exceeds 30 min (catches outages and WSL restarts).
- If a needed skill doesn't exist, say so and flag it as a coding task — do not improvise with a cron or a shell command.

**Price monitor — run location:**
- `price-monitor check` must run from the home WSL2 box, not from the Hetzner VPS. Datacenter IPs are blocked far more aggressively by retail sites. The daily schedule should target the local dispatcher, not the VPS cron.

---

## Security Rules — Hard rules, never break these

- **Never hardcode secrets.** All credentials via `~/.jarvis.env` (local, 600 permissions, WSL2 home) or environment variables (VPS). Never put secrets in `/mnt/c/` paths — DrvFs does not enforce file permissions.
- **Never commit:** `.env`, `~/.jarvis.env`, `/data/`, `/logs/`, `/config/personal/`, any SQLite `.db` file, Home Assistant config
- **Always commit:** `.env.example` with placeholder values, `/config/examples/` with fake data
- **Payment skill:** card token reference only — raw card number never stored, logged, or passed as a string
- **Gmail OAuth2:** request minimum scopes per skill — never request broad access. Gmail skill: read + modify (no send). Calendar: read + write events. Never request full account access.
- **Before any PR to main:** check that no personal data files have been staged
- **Shell is allowlisted with a chaining guard.** The agent's shell access is restricted via `~/.openclaw/workspace/.claude/settings.json`. A **PreToolUse hook** (`~/.openclaw/workspace/scripts/bash_guard.py`) fires before every Bash call and rejects any command containing shell operators: `;`, `&&`, `||`, `|`, `>`, `>>`, `<`, backtick, `$(`, embedded newlines. This forces one-invocation = one-command: no chaining, no piping, no redirection. The explicit deny list covers egress (`git push`, `curl`, `wget`, `ssh`), escape hatches (`bash -c`, `python -c`, `eval`, `source`, `tee`), and destructive ops (`rm -rf`). **Principle: shell invokes capabilities; it is never itself a capability.** Every side-effecting action is a named skill command with its own validation and stage-then-approve. Do not add write/exec primitives to the allow list.
- **Skills and scripts are read-only to the agent.** Edit and Write tools are denied for `skills/`, `scripts/`, `.github/`, `docker-compose.yml`, `requirements*.txt`, `pyproject.toml`, and all instruction files. You write these from Claude Code or your IDE; the agent runs them. See Write Boundary section for the full model.
- **No autonomous push.** The agent may branch and commit locally. `git push` is hard-denied at the tool level and listed as a Red Line in AGENTS.md. Push is always initiated by you.
- **Injection guard is in SOUL.md.** Treat it as the weakest layer — the structural controls above are what actually hold. Do not rely on the instruction layer alone.

---

## Write Boundary & Permission Model

Three tiers, by who holds the pen:

| Tier | Paths | Who may write | How |
|---|---|---|---|
| **Actions** (code/config) | `skills/`, `scripts/`, `.github/`, `docker-compose.yml`, `requirements*.txt`, `pyproject.toml`, `.claude/settings.json`, instruction files (`SOUL.md`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `TOOLS.md`, `persona.md`, `voice.md`) | **AJ / Claude Code only.** Agent tools are denied. | IDE / Claude Code authoring sessions |
| **State** | `/data/jarvis.db`, `/logs/` | Skill process code only | Python `sqlite3` / logging inside an invoked skill. Never raw `sqlite3 "UPDATE…"` from shell. |
| **Memory** | `knowledge/**/*.md` and `~/.jarvis/knowledge/**/*.md` | Agent (via knowledge skill) + AJ / Claude Code | Edit/Write tools scoped to both roots; knowledge skill routes via `TIERS.md` + `os.path.realpath()` canonicalization |

### Permission engine construction

The agent's settings (`~/.openclaw/workspace/.claude/settings.json`) use **default-deny + narrow allow**:

- `deny` always overrides `allow` in Claude Code's permission engine. A broad `deny(**)` would cancel a specific `allow(knowledge/**/*.md)`, so that construction is wrong.
- Correct construction: explicit `allow(knowledge/**/*.md)` + explicit `allow(~/.jarvis/knowledge/**/*.md)` + explicit `deny` for belt-and-suspenders on the highest-value targets + **unlisted = denied** in the non-interactive OpenClaw runtime (the agent runs headlessly; unknown permissions are not prompted, they are denied).
- Result: the only paths the agent can write via Edit/Write tools are `knowledge/**/*.md` (committed) and `~/.jarvis/knowledge/**/*.md` (private).

### Private knowledge root

- Location: `/home/ajpoole/.jarvis/knowledge/` — WSL2 home (ext4), off DrvFs, perms `700` dirs / `600` files
- Contains: `personal/` (profile, goals, school.md program overview, shopping), `people/` (polina, ellie, PEOPLE.md), `home/`, `school/` (SCHOOL.md index + per-course study notes: comp378.md, ...)
- Has a **local-only git repo** for diff/revert — **no remote, never push**
- Domain-to-tier routing defined in committed `knowledge/TIERS.md`; knowledge skill reads it on each call
- History scrub for previously committed personal data: **declined**. Committed-to-date data (school schedule, Ellie food likes) judged non-damaging. Tiering protects *future* data only.

### `.github/workflows/` — highest-value target

CI workflows run with repository secrets and can push code if misconfigured. They must be **absolutely read-only to the agent**. Explicitly denied in settings.json. Never add them to the allow list for any reason.

### Settings files — two contexts, one agent

| File | Who reads it | Purpose |
|---|---|---|
| `~/.openclaw/workspace/.claude/settings.json` | OpenClaw agent runtime | Restrictive: deny-by-default, allow only knowledge writes + skill runner + safe read-only ops |
| `<repo>/.claude/settings.json` | Claude Code (AJ as author) | Permissive: author-level tools, `git pull`, `sudo service`, `python3 -c` for setup tasks |

The agent's working directory is `~/.openclaw/workspace/`. It loads the workspace settings, **not** the repo settings. The workspace settings' `permissions.additionalDirectories` currently includes `~/.jarvis/knowledge` (so the agent's Edit/Write tools can reach the private root). The repo settings' `additionalDirectories` config is a Claude Code author feature only. If `python3 -c` in the repo settings worries you, remove it — it is only needed occasionally during setup.

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
│   ├── gmail-cleanup/         ← Gmail classifier, stage/execute, watch rules
│   ├── calendar/              ← Google Calendar read/write
│   ├── morning-briefing/      ← Daily 7am briefing via schedules dispatcher (posts to Discord itself)
│   ├── home-assistant/        ← Fan + cameras via HA REST API
│   ├── garden/                ← Almanac reminders, logging, Q&A
│   ├── knowledge/             ← FTS5 search + conversational capture loop
│   ├── tasks/                 ← Deadline tracking
│   ├── followups/             ← Active follow-ups + anti-nag engine
│   └── price-monitor/         ← Free price watcher (Shopify/JSON-LD/OG, stdlib only)
├── knowledge/                 ← committed root (no real PII)
│   ├── KNOWLEDGE.md           ← index and schema reference
│   ├── TIERS.md               ← domain tier manifest (committed, not sensitive)
│   ├── garden/                ← almanac
│   ├── recipes/               ← family recipe library (promoted from family/)
│   ├── preferences/           ← generic preferences (voice, food, media — no PII)
│   ├── projects/              ← Altaforma, Bolas
│   └── examples/              ← fake-data schema placeholders for private domains
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
| Phase 3 | 🔄 In progress | Agentic skills — Gmail cleanup ✅, morning briefing ✅, garden ✅, tasks ✅, knowledge (FTS5 + capture loop) ✅, active follow-ups ✅, schedule registry ✅, price monitor (Firecrawl fetch layer) ✅, scheduler firing fix + single cron line + health signals ✅, briefing overhaul (BriefBlock model + news radar + reads coda + interests.md) ✅. Write-boundary hardening ✅. Job search deferred. |
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
- Morning briefing = 1 Sonnet + 2 Haiku calls per day (brief composition + news radar + reads scoring); reads coda adds 1 more Sonnet for the final pick and why-lines

---

## Session Handoff

At the end of each work session, update `CONTEXT.md` with:
1. What was completed (be specific: file paths, function names)
2. What is in progress and where it was left
3. What is blocked and why
4. What is next

Do not skip this step. It is how context survives between sessions.
