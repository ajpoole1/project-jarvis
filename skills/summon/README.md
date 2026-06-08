# summon skill

Launches and manages named builder sessions via `claude remote-control`. Part of the Jarvis dev-loop framework (Phase 2).

## Commands

```
python3 skill.py summon <persona> <id>     Launch RC builder for an authorized item
python3 skill.py ask <id> "<question>"     (builder) post a blocking question to Discord
python3 skill.py watchdog-check <id>       (scheduler) one-shot stall check; prints if stalled
python3 skill.py dismiss <persona>         Kill the tmux session + worktree for a persona
python3 skill.py reaper                    Kill sessions idle past IDLE_MINUTES (default 60)
python3 skill.py status                    List live crew sessions with idle times
```

## Prerequisites

- `tmux` installed
- `claude` CLI v2.1.52+ (`remote-control` support); v2.1.79+ for full subcommand set
- `scripts/dev-crew/launch.sh` (Phase 1 auth wrapper) — handles Max OAuth / clean-env launch
- `knowledge/dev-crew/roster.md` — persona definitions (id, name, repo_dir, auth, permission_mode, rc_spawn)

## How summon works

1. Validates persona exists in roster; validates item id format
2. Checks `claude --version` ≥ 2.1.52
3. Warns if concurrency threshold exceeded
4. Creates a dedicated **detached git worktree** for the item (`jarvis-build-<id>`, a sibling of the repo) so the builder never disturbs the operator's checkout. The worktree inherits the repo's trust, so the unattended start is not blocked by the workspace-trust dialog.
5. Starts a detached tmux session named `crew-<persona>` whose cwd is that worktree
6. Launches `claude --remote-control "<Name> — <id>" --permission-mode auto "<kickoff>"` via the Phase 1 auth wrapper (stdout → `/tmp/claude-rc-<persona>.out`). The kickoff is the **positional prompt** — the interactive RC session auto-submits it on startup. **No `tmux send-keys`** (the prior send-keys-to-console path raced the session and was the bug this replaces).
7. Records the summon in `dev_crew_runs` (jarvis.db) and arms a one-shot `once@` watchdog via the schedules skill
8. Best-effort scrapes the RC URL (15s timeout); session runs regardless and is visible from the Claude mobile app / claude.ai

### Surfacing & stall detection

- **Ambiguity → `ask`.** A builder that hits an under-specified decision runs `ask <id> "<question>"`, which posts to the **main** Discord channel (via `scripts/discord_post.py` → `DISCORD_WEBHOOK_URL`, so it reaches AJ wherever he is) and records `question_at`, then halts. It does not guess.
- **Silent stall → watchdog.** Each summon arms a `once@` schedule for `WATCHDOG_MINUTES` (default 30) later. The heartbeat dispatcher then runs `watchdog-check <id>`; if the build has neither opened a PR / pushed its branch nor posted a question, the check **prints** a stall alert that the dispatcher forwards to Discord, and the one-off retires. `DISCORD_DEVLOOP_WEBHOOK` stays reserved for Tom (QA) verdicts.

`dismiss <persona>` kills the tmux session and removes the persona's build worktree(s).

## Roster format

See `knowledge/dev-crew/roster.md`. Each entry requires:

| Field | Description |
|---|---|
| `id` | Short identifier used as tmux session suffix and summon arg |
| `name` | Display name shown in RC session |
| `repo_dir` | Absolute path to the repo on this machine |
| `auth` | `subscription` or `api` |
| `permission_mode` | `auto` recommended for unattended builds |
| `rc_spawn` | `worktree` (isolated per task) or `same-dir` |

## No venv required

Stdlib-only. Invoke directly with system `python3`.

## Env vars

None. Auth handled by `launch.sh` reading `config/dev-crew/auth-profiles.json`.
