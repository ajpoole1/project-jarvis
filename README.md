# Project Jarvis

A personal AI assistant built on Claude, designed to run 24/7 on a home server and act as an intelligent layer over real life — email, calendar, home automation, garden, and more. Controlled entirely through Discord.

Jarvis also builds itself: a named-builder dev-loop (Claude Code + a non-Claude QA reviewer) authors and ships its own code changes under human authorization, in this same repo.

This is an active portfolio project. Logic is public; personal data stays local.

---

## What it does today

- **Triages Gmail** using Claude Haiku as a classifier. Sender rules are cached in SQLite — repeat senders never hit the API. New senders are classified and staged to Discord for approval before anything is touched. Supports watch rules (semantic alerts on named criteria), archive expiry (per-tag retention policies), real HTTP unsubscribes (RFC 8058 one-click POST + GET fallback), and a flag-and-remove flow for emails Haiku can't confidently classify. Nothing executes without an explicit confirm.

- **Reads and writes Google Calendar** across personal and shared family calendars. Returns formatted daily/weekly views to Discord. Feeds upcoming events into the Gmail classifier so appointment confirmation emails are archived automatically.

- **Controls smart home devices** via Home Assistant. Fan: full local control (mode, speed, temp, child lock) over LocalTuya protocol 3.5. Cameras: 4 Lorex cameras proxied through go2rtc as WebRTC streams in HA.

- **Runs a heartbeat** every 10 minutes during waking hours. Stays silent unless there's something actionable — priority emails, watch rule matches, or uncertain emails needing a decision. Non-priority emails accumulate in a digest queue posted at 13:00 and 18:00.

- **Posts a daily briefing at 7am**: weather, today's calendar, overnight priority inbox, upcoming deadlines, a multi-source salience-ranked news radar, and a personalized "reads coda." Three Discord messages, fired by the `schedules` dispatcher.

- **Tracks garden tasks** with zone-aware reminders, observation logs, and natural-language questions answered from the almanac. Hardiness zone 4–5, Quebec.

- **Maintains a two-root knowledge base** — committed `knowledge/` for generic content (recipes, garden, preferences, projects, dev-loop/dev-standards docs) and a private local root for personal/people/home. FTS5 full-text search across both. The conversational capture loop detects explicit save requests and offers implicit captures for high-signal durable facts, staging every write for approval before anything is written.

- **Tracks deadlines** in SQLite; surfaces upcoming ones in the morning briefing. Tasks and deadlines are AJ's obligations — distinct from Jarvis-owned follow-ups.

- **Manages active follow-ups** — time-conditioned watches Jarvis owns and surfaces proactively. An anti-nag engine gates every proactive outreach: quiet hours, channel-quiet (AJ not mid-conversation), daily budget (max 2/day), and spacing between fires. Each outreach is a single subject, never injected into AJ-initiated threads or the morning briefing. Overflow queues silently; time-sensitive items lapse if the window passes.

- **Tracks personal finances** — stdlib-only CFO layer over SQLite + CSV import: accounts, spend, net worth, debt, runway, and category tagging.

- **Manages a shared grocery list** conversationally, backed by SQLite, synced against a shared calendar note.

- **Monitors prices and travel costs** — watches URLs for price drops (Firecrawl + JSON-LD/OG/regex cascade) and tracks flexible-date flight/hotel costs; both alert-only, never transact.

- **Runs its own dev-loop** — see [Development](#development) below. Jarvis authors specs, a named builder implements them on a branch, a non-Claude reviewer (Tom, on Gemini) gates the PR in CI, and AJ holds both approval gates.

---

## Design philosophy

**Stage then approve.** The agent never takes an irreversible action without a human checkpoint. Propose, present, wait. This applies to email deletion, event creation, code pushes, anything with consequences. Enforced at the skill level — it can't be bypassed by a confused agent.

**Graceful degradation.** Never fail silently. If a skill can't run, surface the problem. If a classification is uncertain, flag it for the user rather than guess. Always leave the user better off than before the run.

**Single surface.** Everything flows through Discord. No separate apps, no dashboards to check. One place, one context.

**Security by design.** Secrets never touch the repo. Personal data never leaves the local layer. The public codebase exposes logic, never state. OAuth tokens, SQLite databases, and personal config are all gitignored. Code-write authority is structurally bounded: Jarvis (the orchestrator) can only write Markdown under the knowledge tree; the builder persona gets broad repo access but is hard-denied from touching `main`, merging, or force-pushing.

**Heterogeneous QA.** The dev-loop's reviewer (Tom) is deliberately a different model family (Gemini) from the builder (Claude) — a same-family reviewer shares the builder's blind spots. Fail-closed: if Tom can't run, the check is red, never green.

**Cost discipline.** Claude Haiku for classification and rule-based decisions. Claude Sonnet for reasoning and drafting. Never Opus for automated tasks. Target: $5–15/month at steady state for the assistant; the dev-loop's cost is separate (Claude Code subscription + a small Gemini API spend cap).

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

Scheduled tasks (heartbeat, digest, briefing) run via the `schedules` skill's dispatcher — an agent-safe cron that only invokes allowlisted skills — and post to Discord directly via webhook; they do not go through OpenClaw. A couple of jobs (follow-ups, the nightly checkpoint/restart) still run as raw WSL cron entries and haven't migrated onto the dispatcher yet.

Jarvis's own identity, rules, and always-on context are delivered via a separate mechanism (the **spine** — see below), not through OpenClaw's own bootstrap injection, which proved unreliable on the current backend.

### Infrastructure layers

| Layer | Where | What runs |
|---|---|---|
| Local brain | Windows PC / WSL2 | OpenClaw, Python skills, SQLite, the dev-loop |
| Home hardware controller | Raspberry Pi 4 (`ha-pi`, dedicated, own VLAN) | Home Assistant (Container, not HA OS) + Frigate NVR (Coral USB TPU for on-device detection) + Mosquitto, all in Docker Compose. 4 Lorex cameras pulled by RTSP; 24/7 recording with 5-day retention, 30-day event-clip retention. |
| Client | `jarvis-app` (separate repo) | React Native/Expo Android client talking to the OpenClaw gateway's WebSocket — see [The app](#the-app) below |

There's no cloud tier today — everything runs on the local network.

### Key decisions

- **SQLite as shared memory.** Skills query on demand. Never preloaded into Claude context.
- **Python for all skill logic.** One virtualenv per skill (only when a skill has third-party dependencies — stdlib-only skills skip the venv). OpenClaw shells out via bash.
- **Skills are isolated.** Cross-skill communication happens via subprocess JSON, not imports.
- **Home Assistant as the smart home API.** One skill controls all devices. Never bypass HA.
- **Frigate + Coral for detection, not cloud NVR.** Local-only recording and object detection — no footage leaves the network. Detect runs on cheap sub-streams; the Coral TPU handles inference well under its 70–100 inf/sec ceiling.

### The app

Discord is the control surface today, but a dedicated client is in progress in a **separate repo, `jarvis-app`** — a React Native (Expo) + TypeScript Android client, not the Flutter app the original roadmap assumed. It talks directly to the OpenClaw gateway's node WebSocket (not through Discord), authenticating with an Ed25519 keypair generated on first run and paired via a setup code. It connects to Jarvis's **shared main session** — the same brain and memory as the Discord and CLI surfaces, so it's one continuous assistant, not a separate instance with its own history.

Currently at milestone **M6 (Polish)** of its build plan: the skeleton, gateway reachability, auth handshake, and a working chat surface (M1–M5) are done; M6 is adding auto-reconnect, multi-session/channel visibility, and disconnect/retry UX. See `jarvis-app`'s own README for the full build plan and connection protocol details — not duplicated here since it's a separate repo with its own release cycle.

---

## Where things stand

The original phase roadmap (below, kept for history) has largely been overtaken by how the project actually grew — infra and the app both moved ahead of where the plan assumed they would, and the cloud tier dropped out entirely:

- **Home hardware controller: done, live** on a Pi 4, not deferred to a future Pi5/Phase-4 milestone (details above).
- **Cloud brain: off the table.** No VPS, no cloud tier — everything stays local.
- **Client app: in progress** in `jarvis-app`, further along than a stub (details above).
- **Everything else in Phase 3** (agentic skills) is done: morning briefing, garden, tasks, knowledge (FTS5 + capture loop), active follow-ups, finance, grocery, price/travel monitors, and the dev-loop + spine are all live.

<details>
<summary>Original phase roadmap (historical — no longer actively tracked)</summary>

| Phase | Status | Goal |
|---|---|---|
| Phase 1 | ✅ Done | Foundation — OpenClaw, Discord bot, dev environment |
| Phase 2 | ✅ Done | Core integrations — Gmail ✅, Calendar ✅, Home Assistant (fan + cameras) ✅ |
| Phase 3 | ✅ Done | Agentic skills — see the bullet list above |
| Phase 4 | ✅ Superseded | Originally "VPS migration + Pi deployment" — the Pi half shipped (see Infrastructure layers above); the VPS/cloud half was dropped |
| Phase 5 | 🔄 In progress | Jarvis client app — now React Native/Expo (not Flutter), building in `jarvis-app` |

</details>

---

## Skills

| Skill | Description |
|---|---|
| `gmail-cleanup` | Haiku classifier, SQLite rule cache, stage/execute/adjust, watch rules, archive expiry, flag-and-remove, real unsubscribe |
| `calendar` | Google Calendar read/write, multi-calendar, Discord formatting, JSON output for integrations |
| `morning-briefing` | Salience-ranked morning brief: weather, calendar, priority inbox, deadlines, news radar, personalized "reads coda" |
| `home-assistant` | Entity control/read via HA REST API (fan: LocalTuya LAN protocol 3.5 — mode, speed, temp, child lock; cameras: 4 Lorex via go2rtc WebRTC) |
| `garden` | Zone-aware reminders, observation logging, natural-language almanac queries. Zone 4–5, Quebec |
| `knowledge` | Search/update/append across the two-root knowledge model, with staged capture/approval and undo |
| `tasks` | Deadline tracking against the shared `jarvis.db`; surfaced in morning briefing |
| `followups` | Jarvis-owned, time-conditioned intentions; anti-nag engine gates all proactive outreach |
| `finance` | Stdlib-only personal CFO — SQLite + CSV import: accounts, spend, net worth, debt, runway, tagging |
| `grocery` | Conversational shopping list, SQLite-backed, synced against a shared calendar note |
| `media` | Watched-media store — tracks titles per person, filters seen/bounced items from recommendations |
| `price-monitor` | Watches URLs for price drops (Firecrawl + JSON-LD/OG/regex cascade); degrades to follow-ups on failure |
| `travel-monitor` | Flexible-date travel cost tracker (flights/Amadeus, hotels/Firecrawl); alerts only, never books |
| `reminder` | Prints a free-text reminder to stdout; the dispatcher forwards it to Discord |
| `schedules` | Agent-safe cron — Jarvis proposes a schedule, AJ approves via Discord; the dispatcher only runs allowlisted skills |
| `devqueue` | Validate / push / close dev-notes queue items against the `dev-queue` branch (see [Development](#development)) |
| `devloop` | Read-only narration of dev-loop state across repos — open/merged PRs, standup summaries |
| `summon` | Launches and manages named builder sessions (e.g. Herr Mannkusser) via `claude remote-control` |
| `spine-compile` / `spine-check` | Daily nonce rotation + MEMORY.md rebuild, and the freshness verifier that alerts if delivery silently breaks (see [The spine](#the-spine-how-jarviss-identity-actually-loads)) |
| `gateway-watchdog` | Samples the OpenClaw gateway's memory (RSS) every 10 minutes; alerts Discord over threshold as an early OOM warning |

---

## Stack

| Component | Technology |
|---|---|
| Agent runtime | [OpenClaw](https://openclaw.ai) |
| AI models | Claude Haiku (classification) · Claude Sonnet (reasoning) · Claude Sonnet 5 (dev-loop builder) · Gemini (dev-loop QA reviewer, decorrelated from the builder) |
| Skill language | Python 3.12 |
| Persistent memory | SQLite |
| Control surface | Discord |
| Scheduled tasks | `schedules` skill dispatcher (agent-safe cron) + a couple of raw WSL cron jobs → Discord webhook |
| Smart home | Home Assistant + Frigate + Coral TPU, on a dedicated Raspberry Pi 4 |
| CI/CD | GitHub Actions — security scan, ruff + pytest, docker validation, dev-queue schema validation, Tom QA gate, merge cleanup |

---

## Knowledge base

Two roots, routed by whether content could identify or locate a specific real person:

- **Committed** (`knowledge/`, this repo, no PII) — generic reference content: `garden/` (almanac), `recipes/`, `preferences/`, `projects/`, plus the dev-loop's own documentation tree: `architecture/` (spine design), `dev-crew/` (builder roster + persona overlays), `dev-notes/` (the `queue/`/`backlog/`/`archive/` dev-loop item lifecycle, mirrored onto the `dev-queue` branch), and `parked/`.
- **Private** (`~/.jarvis/knowledge/`, own local git repo, never mirrored, mode 700) — `people/` (per-person cards + a registry), `home/`, `personal/`, `school/`.

`knowledge/KNOWLEDGE.md` is the index and schema; `knowledge/TIERS.md` defines the committed/private routing rule. The `knowledge` skill enforces the boundary in code (path-realpath + tier assertion) — writes can't land in the wrong root by accident. FTS5 gives full-text search across both roots; nothing is preloaded into every call, everything is fetched on demand.

---

## Development

Jarvis isn't just the assistant — this repo is also where Jarvis's own code changes get authored and shipped, through a small dev-loop with two human gates.

### The actors

- **Jarvis** (orchestrator) — brainstorms specs from real friction hit in daily use, but never writes code itself; it can only write Markdown under the knowledge tree.
- **The operator** (AJ) — holds both gates: authorizing a spec, and merging the resulting PR.
- **The builder** — a named Claude Code persona (currently **Herr Mannkusser**, defined in `knowledge/dev-crew/roster.md`) that implements an authorized spec on a feature branch. Broad repo write access, but hard-denied from touching `main`, merging, or force-pushing.
- **Tom** — the QA reviewer, running in CI on Gemini. Deliberately a different model family from the builder (Claude) so the review doesn't share the builder's blind spots. Fail-closed: if Tom can't produce a verdict (including after exhausting an optional Claude fallback used only when Gemini itself is down), the check is red — never green by default.

### The loop

1. **Spec.** A gap gets written up as a dev-notes item (`knowledge/dev-notes/queue/<id>.md`, YAML frontmatter + Intent/Scope/Acceptance-criteria body) and pushed to the `dev-queue` branch via `devqueue push` — gated behind AJ's explicit ✅ approval (`devqueue approve <id> <discord-user-id>`, one approval covers one push, once).
2. **Authorize (gate #1).** AJ authorizes the spec.
3. **Summon.** `summon <persona> <id>` spawns the named builder in a dedicated git worktree on `feature/<id>`.
4. **Build.** The builder implements the spec literally, runs `scripts/dev-loop/checks.sh` (ruff + pytest — the same command CI runs) before every commit, and lands via `/ship` (checks → `/qa` self-review → commit + push → PR).
5. **Tom QA.** The PR triggers `.github/workflows/qa.yml` (via the scoped caller `qa-caller.yml`) — Tom reviews the diff against the repo's declared QA lenses and posts a verdict comment. Blocking findings fail the check; advisory findings become backlog stubs on merge.
6. **Merge (gate #2).** AJ reviews and merges. `merged.yml` then flips the item's status to `merged`, moves it `queue/` → `archive/` on `dev-queue`, and seeds backlog stubs from Tom's advisory notes.
7. **Close, if never built.** An authorized item that's abandoned or superseded (not merged) is closed instead: `devqueue close <id> "<reason>"` (alias `archive`) — same hard-lock and approval gate as `push`, sets `status: closed` with a mandatory `closed_reason`.

### The standards

- `docs/dev-standards/DEV_BASE.md` — the repo-agnostic development constitution: common law (spec-first, halt-don't-guess, never fail silent), git discipline, the three ritual verbs, and the QA contract's shared finding vocabulary (`spec_conformance` / `defects` / `architecture_notes`).
- `docs/dev-standards/QA_LENSES.md` — the review lenses every QA pass is graded against: structural (spec conformance, correctness, failure behavior, security, test adequacy, scope/simplicity) plus per-repo convention lenses declared in `CHARTER.md`.
- `docs/dev-standards/PY_STANDARDS.md` / `docs/guides/SKILL_GUIDE.md` — this repo's Python conventions and the skill-authoring pattern (`skills/<name>/skill.py` + `requirements.txt` + `README.md`, stdlib-only skills skip the venv).
- `CHARTER.md` — this repo's specific values: stack, QA pairing (builder model vs. reviewer model), QA parameters, and which convention docs count as QA lenses here.
- Three ritual skills tie it together: **`/ship`** (checks → `/qa` → commit+push → PR — the only sanctioned way code leaves a branch), **`/qa`** (lens-based review, runs standalone or inside `/ship`), **`/handoff`** (scaffolds a session entry in `CONTEXT.md`, run at every session end).

### The spine — how Jarvis's identity actually loads

OpenClaw's own context-bootstrap injection proved unreliable on the current backend (project-scope config never loads for Jarvis — only user-scope `~/.claude`). So Jarvis's identity, hard rules, and always-on routing are delivered instead through **Claude Code SessionStart hook scripts** plus the auto-memory `MEMORY.md` file — the only two channels that have proven reliable. Everything else (people cards, topic knowledge) is fetched on demand via the knowledge skill.

A daily **compiler** (`scripts/spine_compile.py`) regenerates the delivered artifacts from human-owned source files and rotates a nonce through them; a daily **checker** (`scripts/spine_check.py`) asks the live agent to echo that nonce, alerting to Discord if delivery ever silently breaks. The nonce is a delivery canary, not a secret — a fixed sentinel gets pattern-completed from memory by the model even when delivery is actually broken, so only a value that changes daily proves a chunk really loaded. A drift-guard lint cross-checks the hook registry (`~/.claude/settings.json`) against what the compiler/checker actually know about, so a hook that's silently stopped firing gets caught the same day, not discovered by accident.

Full design rationale: `knowledge/architecture/SPINE_ARCHITECTURE.md`.

### Workflow: local change → live deploy

```
edit code / write spec
        │
        ▼
scripts/dev-loop/checks.sh   (ruff + pytest — must be green)
        │
        ▼
/ship  →  git push feature/<id>  →  gh pr create
        │
        ▼
CI on the PR:
  Python Quality · Security Scan · Docker Validation
  Tom QA (qa-caller.yml → qa.yml)  — blocking findings fail the check
        │
        ▼
AJ reviews + merges PR into main   (gate #2)
        │
        ▼
merged.yml: flip dev-queue item to `merged`, archive it, seed backlog
        │
        ▼
jarvis-deploy  (run on the host, AJ's action — not automatic)
  pulls origin/main into /opt/jarvis-live, rebuilds any changed skill venvs
        │
        ▼
systemctl --user restart openclaw-gateway   (or: openclaw gateway restart)
  Jarvis's live session restarts and picks up the deployed code + any
  instruction-file edits (these only take effect after a restart, since
  Jarvis runs as a persistent long-lived session, not a fresh process per turn)
```

Chain of custody is deliberate: Jarvis can't push to `main` under any circumstance; the builder persona can only push to its own feature branch; only AJ merges, deploys, and restarts the gateway.

---

## CI/CD

Every PR into `main` runs:

| Workflow | What it checks |
|---|---|
| Security Scan | TruffleHog (full history) + Gitleaks |
| Python Quality | Ruff lint + format · pytest |
| Docker Validation | `docker compose config` against `.env.example` |
| QA Gate (`qa-caller.yml` → `qa.yml`) | Tom (Gemini) reviews the diff against the repo's QA lenses; scoped to PRs touching `skills/`, `docs/dev-loop/`, `scripts/dev-loop/`, `scripts/dev-crew/`, or labelled `dev-loop`. Fail-closed. |

On the `dev-queue` branch specifically, **Dev-Queue Schema Validator** checks every queue item's frontmatter against the schema. On merge to `main`, **Dev-Loop Merge Cleanup** (`merged.yml`) archives the corresponding dev-queue item and seeds backlog stubs from any advisory QA notes.

---

## Running locally

```bash
# Clone and set up dev environment
git clone https://github.com/altaforma-conseils/project-jarvis.git
cd project-jarvis
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-dev.txt

# Run the same checks CI runs
scripts/dev-loop/checks.sh

# Set up a skill that has third-party dependencies
cd skills/gmail-cleanup
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt

# Copy and fill in env vars
cp .env.example .env
```

See each skill's `README.md` for credential setup and first-run instructions. See `docs/dev-loop/BUILD_RUNBOOK.md` for the builder-persona procedure if you're driving the dev-loop rather than a single skill.

---

## Repository structure

```
project-jarvis/
├── .claude/skills/             # Ritual skills: /ship, /qa, /handoff
├── .github/workflows/          # Security scan, Python quality, Docker validation,
│                               # dev-queue schema validator, Tom QA gate, merge cleanup
├── CHARTER.md                  # This repo's dev-loop values (stack, QA pairing, lenses)
├── CLAUDE.md                   # Thin author-session overlay — pointers only, no restated rules
├── CONTEXT.md                  # Session handoff log (/handoff appends here)
├── docs/
│   ├── dev-standards/          # DEV_BASE.md (constitution), QA_LENSES.md, PY_STANDARDS.md
│   ├── dev-loop/               # DEV_LOOP_REFERENCE.md, BUILD_RUNBOOK.md
│   ├── guides/                 # SKILL_GUIDE.md
│   └── ARCHITECTURE.md         # Why Jarvis exists, security model, phase status
├── knowledge/                  # Committed knowledge root (no PII)
│   ├── KNOWLEDGE.md            # Index and schema
│   ├── TIERS.md                # Committed/private routing rule
│   ├── architecture/           # SPINE_ARCHITECTURE.md
│   ├── dev-crew/               # Builder roster + persona overlays
│   ├── dev-notes/              # queue/ · backlog/ · archive/ (mirrors dev-queue branch)
│   ├── garden/ recipes/ preferences/ projects/ parked/
│   └── examples/               # Fake-data schema for private domains
├── scripts/
│   ├── discord_post.py         # Webhook poster (stdlib, chunks at 1900 chars)
│   ├── cron_followups.sh        # Raw WSL cron entry — one of two jobs not yet on the schedules skill
│   ├── cron_nightly_checkpoint.sh  # The other — 4am session-transcript capture + gateway restart
│   ├── spine_compile.py         # Daily nonce rotation + MEMORY.md rebuild
│   ├── spine_check.py           # Daily freshness verifier
│   └── dev-loop/                # checks.sh, open-pr.sh, start-build.sh, discord_notify.py
├── skills/                      # One skill.py per capability (see Skills table above)
├── config/
│   ├── personal/                # gitignored — OAuth tokens, personal config
│   └── examples/                # committed — fake data showing structure
├── data/                        # gitignored — SQLite DB
└── logs/                        # gitignored
```
