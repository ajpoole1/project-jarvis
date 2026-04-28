# Jarvis — Session Context

_Update this file at the end of every Claude Code session. This is how context survives between sessions._

---

## Current Phase

**Phase 2 — Core integrations** (in progress)

---

## Last Updated

2026-04-28 — Calendar skill built, Gmail stage/execute flow enforced, Discord UX improved.

---

## Recently Completed

- Installed WSL2, Docker Desktop, Node.js (via nvm v24.15.0), Claude Code CLI in WSL
- Created Python venv at `.venv`, installed ruff + pytest dev dependencies
- Installed and configured OpenClaw — Claude CLI auth, Sonnet as default model
- Created private Discord server, registered Jarvis bot, enabled Message Content Intent
- Connected OpenClaw to Discord — bot online and responding in Jarvis server
- Built `skills/gmail-cleanup/skill.py` — full Gmail cleanup skill:
  - Haiku classifier with SQLite sender rule cache (`/data/jarvis.db`)
  - Actions: archive, trash, unsubscribe, keep
  - Tagging system: `jarvis/*` Gmail labels created and applied on execute
  - Stage/execute/cancel/adjust/pending commands — true stage-then-approve via SQLite
  - `NEVER_CACHE_SENDERS` — subject-aware senders (amisgest) always go through Haiku
  - Calendar context injection — upcoming 30-day events passed to Haiku prompt
  - Env vars loaded from `.env` automatically via python-dotenv
- Built `skills/calendar/skill.py` — Google Calendar integration:
  - Commands: today, week, check <date>, upcoming <days>, add, calendars
  - Read/write scope, reuses gmail_credentials.json, separate calendar_token.json
  - Calendars: primary + Home (shared with Polina)
  - Timezone: America/Toronto
- Fixed OpenClaw heartbeat — HEARTBEAT.md now has explicit bash command, not abstract skill name
- Fixed OpenClaw auth — switched from expiring OAuth to API key (permanent, no re-auth)
- Set up `gh` CLI for git push/PR from WSL
- Discord UX — Jarvis responds without @mention in private server, conversational context per channel
- Cleared ~4,500+ email backlog from inbox, Updates, Promotions, Social, Forums tabs
- PR #3 merged to main

---

## In Progress

Nothing actively in progress — clean state at end of session.

---

## Blocked

Nothing blocked.

---

## Up Next

### Gmail (remaining)
- [ ] Observe tagging system over real emails — spot-check `jarvis/*` labels in Gmail
- [ ] Gmail: smart heartbeat — 10-min silent checks during work day, buffer results to JSON, digest 3x/day; immediate ping for: family tag, security alerts, financial alerts, real person emails
- [ ] Gmail: dynamic watch rules — `gmail_watches` SQLite table, AJ tells Jarvis "watch for X", Haiku semantic match, immediate Discord ping
- [ ] Gmail: archive purge/expiry — trash archived emails older than N days by tag (projects: 30d, shipping: 60d, receipts: 90d)
- [ ] Gmail: flag-and-remove — emails Jarvis isn't sure about get flagged in Discord for AJ to decide
- [ ] Gmail + Calendar: appointment-aware classifier already built — observe it in the wild
- [ ] Unsubscribe: currently just trashes — consider parsing List-Unsubscribe header for real unsubscribe

### Phase 2 (next skills)
- [ ] Morning briefing — single Sonnet call at 9am: calendar events, inbox flags, overnight Jarvis activity
- [ ] Home Assistant skill — Phase 2
- [ ] Spotify skill — Phase 2

### Infrastructure
- [ ] Build `openclaw/` Dockerfile so `docker-compose.yml` openclaw service can actually build
- [ ] Disable Gmail tabs (inbox type change) — tabs disabled but some residual CATEGORY_* labels still need drain_categories periodically

---

## Architecture Decisions

- Default branch is `main`. Strategy: `feature/skill-name` → `develop` → `main` (protected).
- CI check names in branch protection must match job `name:` fields in workflow YAMLs exactly.
- `docker-compose.yml` uses a `busybox` placeholder until real `openclaw` Dockerfile exists.
- SQLite at `/data/jarvis.db` — gitignored, shared across all skills.
- Python venv per skill in `skills/<name>/.venv` — not committed.
- OpenClaw workspace files (`USER.md`, `TOOLS.md`, `HEARTBEAT.md`, `AGENTS.md`) at `~/.openclaw/workspace/` — not in repo, configure manually on new machines.
- Gmail OAuth token at `/config/personal/gmail_token.json` — gitignored, stays local.
- Calendar OAuth token at `/config/personal/calendar_token.json` — gitignored, stays local.
- GMAIL_DRY_RUN env var still exists but the skill's stage command always stages first regardless.
- OpenClaw uses API key auth (not OAuth) — configured via `openclaw configure`, stored in `~/.openclaw/agents/main/agent/auth-profiles.json`.
- gh CLI installed in WSL for git push/PR operations.

---

## Known Issues / Intentional Oddities

- `docker.yml` openclaw service has `build: ./openclaw` which doesn't exist — compose validation passes because Docker only validates syntax, not build contexts.
- Gmail's algorithm may re-categorize some emails even after disabling tabs. Run `drain_categories` periodically if new emails appear stuck in CATEGORY_* labels.
- `message@amisgest.com` is in NEVER_CACHE_SENDERS — always goes through Haiku, never cached. Daycare uses same sender for journal de bord (trash) and real communications (keep/family).
- Unsubscribe action currently just trashes — does not actually hit unsubscribe URLs. Future enhancement.
- OpenClaw heartbeat HEARTBEAT.md fires 3x/day — runs gmail-cleanup in dry run and pings Discord only if actionable emails found.
