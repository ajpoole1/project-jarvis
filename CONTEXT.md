# Jarvis — Session Context

_Update this file at the end of every Claude Code session. This is how context survives between sessions._

---

## Current Phase

**Phase 2 — Core integrations** (in progress)

---

## Last Updated

2026-04-27 — Gmail cleanup skill built and wired into Discord. Inbox cleared.

---

## Recently Completed

- Installed WSL2, Docker Desktop, Node.js (via nvm v24.15.0), Claude Code CLI in WSL
- Created Python venv at `.venv`, installed ruff + pytest dev dependencies
- Installed and configured OpenClaw — Claude CLI auth, Sonnet as default model
- Created private Discord server, registered Jarvis bot, enabled Message Content Intent
- Connected OpenClaw to Discord — bot online and responding to @mentions in Jarvis server
- Built `skills/gmail-cleanup/skill.py` — full Gmail cleanup skill:
  - Haiku classifier with SQLite sender rule cache (`/data/jarvis.db`, table `gmail_sender_rules`)
  - Actions: archive, trash, unsubscribe, keep
  - Tagging system: `jarvis/*` Gmail labels created and applied on execute
  - Commands: `run`, `drain`, `drain_categories`, `purge_archive`, `review`, `confirm_action`, `confirm_all`, `override`
  - Env vars loaded from `.env` automatically via python-dotenv
  - Stage-then-approve enforced: GMAIL_DRY_RUN=true by default
- Cleared ~4,500+ email backlog from inbox, Updates, Promotions, Social, Forums tabs
- Wired Gmail skill into OpenClaw via `USER.md` and `TOOLS.md` in workspace
- Jarvis responds to natural language ("clean my inbox") and runs skill via bash
- HEARTBEAT.md configured for 3x/day Gmail checks with silent-if-empty rule

---

## In Progress

**Gmail cleanup skill** — `skills/gmail-cleanup/skill.py`
- Skill is live and working from Discord
- Tagging system built (jarvis/receipts, bills, job-search, health, family, projects) — not yet proven on real emails
- Heartbeat instructions written in HEARTBEAT.md — **OpenClaw heartbeat poller not yet verified active**

---

## Blocked

- **OpenClaw heartbeat interval** — need to check `/gateway/config-agents` in the OpenClaw control UI to confirm heartbeat is enabled and firing at the right cadence. Without this, Gmail auto-checks won't run.

---

## Up Next

### Gmail (remaining)
- [ ] Verify OpenClaw heartbeat poller is active at `/gateway/config-agents`
- [ ] Observe tagging system over 1 week of real emails — spot-check `jarvis/*` labels in Gmail
- [ ] Gmail: smart heartbeat — 10-min silent checks during work day, buffer results to JSON, digest 3x/day; immediate ping for: family tag, security alerts, financial alerts, real person emails. Build after tagging is proven.
- [ ] Gmail: dynamic watch rules — `gmail_watches` SQLite table, AJ tells Jarvis "watch for X", Haiku semantic match, immediate Discord ping. Fields: description, expires_at, channel_id, triggered_at.
- [ ] Gmail: archive purge/expiry — trash archived emails older than N days by tag (projects: 30d, shipping: 60d, receipts: 90d)
- [ ] Gmail: flag-and-remove — emails Jarvis isn't sure about get flagged in Discord for AJ to decide
- [ ] Gmail + Calendar: appointment-aware classifier — check Google Calendar before archiving appointment emails

### Phase 2 (next skills)
- [ ] Calendar skill — needed to unblock appointment-aware classifier and morning briefing
- [ ] Morning briefing — single Sonnet call at 9am: calendar events, inbox flags, overnight Jarvis activity
- [ ] Home Assistant skill — Phase 2
- [ ] Spotify skill — Phase 2

### Infrastructure
- [ ] Build `openclaw/` Dockerfile so `docker-compose.yml` openclaw service can actually build
- [ ] Disable Gmail tabs (inbox type change) — still pending, Gmail keeps re-categorizing emails into Updates/Promotions

---

## Architecture Decisions

- Default branch is `main`. Strategy: `feature/skill-name` → `develop` → `main` (protected).
- CI check names in branch protection must match job `name:` fields in workflow YAMLs exactly.
- `docker-compose.yml` uses a `busybox` placeholder until real `openclaw` Dockerfile exists.
- SQLite at `/data/jarvis.db` — gitignored, shared across all skills.
- Python venv per skill in `skills/<name>/.venv` — not committed.
- OpenClaw workspace files (`USER.md`, `TOOLS.md`, `HEARTBEAT.md`) at `~/.openclaw/workspace/` — not in repo, configure manually on new machines.
- Gmail OAuth token at `/config/personal/gmail_token.json` — gitignored, stays local.
- GMAIL_DRY_RUN=false in `.env` for live runs — default is true (safe).

---

## Known Issues / Intentional Oddities

- `docker.yml` openclaw service has `build: ./openclaw` which doesn't exist — compose validation passes because Docker only validates syntax, not build contexts.
- Gmail's algorithm re-categorizes some emails (e.g. GitHub notifications → Updates tab) even after processing. Run `drain_categories` periodically until Gmail tabs are fully disabled.
- OpenClaw heartbeat: HEARTBEAT.md is configured but the poller interval is unverified — check gateway config before assuming auto-checks are running.
- `message@amisgest.com` emails with subject "journal de bord" should be trashed (Ellie's school app notification — redundant). Other subjects from same sender are family-priority keep. Handled in Haiku prompt, NOT in SQLite cache.
