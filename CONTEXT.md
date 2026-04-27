# Jarvis — Session Context

_Update this file at the end of every Claude Code session. This is how context survives between sessions._

---

## Current Phase

**Phase 1 — Foundation** (not started)

---

## Last Updated

2026-04-27 — CI/CD setup, branch rename, branch protection

---

## Recently Completed

- Created `.github/workflows/security.yml` — TruffleHog (full history, verified-only) + gitleaks
- Created `.github/workflows/quality.yml` — Ruff lint + format check + pytest, Python 3.11/3.12 matrix
- Created `.github/workflows/docker.yml` — `docker compose config` validation using `.env.example`
- Created `pyproject.toml` — Ruff config targeting Python 3.11+, rules: E/W/F/I/B/C4/UP
- Created `requirements-dev.txt` — pinned ruff 0.4.4, pytest 8.2.0, pytest-cov 5.0.0
- Created `.env.example` — placeholder values for all integrations (Anthropic, Discord, Gmail, HA, Spotify, Tailscale)
- Created `docker-compose.yml` — scaffold with openclaw service + busybox placeholder
- Renamed `master` → `main` on GitHub and locally
- Created `develop` branch, pushed to remote
- Set branch protection on `main`: all 5 CI checks required, strict up-to-date, force push/delete blocked

---

## In Progress

_Nothing. Phase 1 foundation work is next._

---

## Blocked

_Nothing._

---

## Up Next

- [ ] Install WSL2 Remote extension in VS Code
- [ ] Confirm Docker Compose starts cleanly from WSL terminal
- [ ] `npm install -g openclaw`, run onboard wizard, set Claude API key
- [ ] Create private Discord server, register bot token
- [ ] Connect Discord integration, test first voice note → Claude response
- [ ] Build `openclaw/` Dockerfile so `docker-compose.yml` openclaw service can actually build
- [ ] Create `tests/` directory with a placeholder test so pytest doesn't fail on empty suite

---

## Architecture Decisions Made This Session

- Default branch is `main` (not `master`). Branch strategy: `feature/skill-name` → `develop` → `main`.
- CI check names in branch protection must match job `name:` fields in workflow YAMLs exactly.
- `docker-compose.yml` uses a `busybox` placeholder service until the real `openclaw` Dockerfile exists.

---

## Known Issues / Intentional Oddities

- `quality.yml` pytest will fail until a `tests/` directory with at least one test file exists — create this before the first PR to `develop`.
- `docker.yml` `openclaw` service has `build: ./openclaw` which doesn't exist yet — compose validation passes today because Docker only validates syntax, not build contexts.
