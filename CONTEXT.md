# Jarvis — Session Context

_Update this file at the end of every Claude Code session. This is how context survives between sessions._

---

## Current Phase

**Phase 2 — Core integrations** (in progress)

---

## Last Updated

2026-04-27 — Phase 1 complete: OpenClaw + Discord bot live and responding

---

## Recently Completed

- Created `.github/workflows/security.yml` — TruffleHog (full history, verified-only) + gitleaks
- Created `.github/workflows/quality.yml` — Ruff lint + format check + pytest, Python 3.11/3.12 matrix
- Created `.github/workflows/docker.yml` — `docker compose config` validation using `.env.example`
- Created `pyproject.toml` — Ruff config targeting Python 3.11+, rules: E/W/F/I/B/C4/UP
- Created `requirements-dev.txt` — pinned ruff 0.4.4, pytest 8.2.0, pytest-cov 5.0.0
- Created `.env.example` — placeholder values for all integrations (Anthropic, Discord, Gmail, HA, Spotify, Tailscale)
- Created `docker-compose.yml` — scaffold with openclaw service + busybox placeholder
- Added `tests/test_placeholder.py` — prevents pytest exit-code-5 on empty suite
- Renamed `master` → `main` on GitHub and locally
- Created `develop` branch, pushed to remote
- Set branch protection on `main`: all 5 CI checks required, strict up-to-date, force push/delete blocked
- Installed WSL2, Docker Desktop, Node.js (via nvm v24.15.0), Claude Code CLI in WSL
- Created Python venv at `.venv`, installed ruff + pytest dev dependencies
- Installed and configured OpenClaw — Claude CLI auth, Sonnet as default model
- Created private Discord server, registered Jarvis bot, enabled Message Content Intent
- Connected OpenClaw to Discord — bot online and responding to @mentions in Jarvis server

---

## In Progress

**Gmail cleanup skill** — `skills/gmail-cleanup/skill.py`
- Inbox cleared. Classifier running with confirmed SQLite rule cache.
- Tagging system designed but not yet built (see Up Next)

---

## Blocked

_Nothing._

---

## Up Next

- [ ] Build `openclaw/` Dockerfile so `docker-compose.yml` openclaw service can actually build
- [ ] Begin Phase 2: Home Assistant skill, Calendar, Spotify
- [x] Gmail: clear inbox backlog — drain, drain_categories, purge_archive all working
- [ ] Gmail: build tagging system — apply Gmail labels alongside actions:
    - jarvis/receipts, jarvis/bills, jarvis/job-search, jarvis/health, jarvis/family, jarvis/projects
    - Haiku returns both action + tag in one call
    - Create labels in Gmail if they don't exist, apply on execute
- [ ] Gmail: flag-and-remove flow — emails Jarvis isn't sure about get flagged in Discord for user decision then removed
- [ ] Gmail: add `purge` mode — scan archive for emails older than N days, re-classify with "still relevant?" prompt, stage trash for expired notifications (project shutdowns, shipping, CI failures, surveys). Run after inbox is clean.
- [ ] Gmail: wire skill into OpenClaw so Jarvis can trigger cleanup from Discord
- [ ] Gmail + Calendar: calendar-aware classifier — appointment confirmations/reminders check Google Calendar before acting:
    - Event exists → archive silently
    - Event missing → flag in Discord, offer to add to calendar (stage then approve)
    - Update/cancellation emails → always flag, never silently archive
    - Requires Calendar skill built first

---

## Architecture Decisions Made This Session

- Default branch is `main` (not `master`). Branch strategy: `feature/skill-name` → `develop` → `main`.
- CI check names in branch protection must match job `name:` fields in workflow YAMLs exactly.
- `docker-compose.yml` uses a `busybox` placeholder service until the real `openclaw` Dockerfile exists.

---

## Known Issues / Intentional Oddities

- `quality.yml` pytest will fail until a `tests/` directory with at least one test file exists — create this before the first PR to `develop`.
- `docker.yml` `openclaw` service has `build: ./openclaw` which doesn't exist yet — compose validation passes today because Docker only validates syntax, not build contexts.
