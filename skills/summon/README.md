# summon skill

Launches and manages named builder sessions via `claude remote-control`. Part of the Jarvis dev-loop framework (Phase 2).

## Commands

```
python3 skill.py summon <persona> <id>   Launch RC session for an authorized item
python3 skill.py dismiss <persona>       Kill the tmux session for a persona
python3 skill.py reaper                  Kill sessions idle past IDLE_MINUTES (default 60)
python3 skill.py status                  List live crew sessions with idle times
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
4. Starts a detached tmux session named `crew-<persona>`
5. Launches `claude remote-control --name "<Name> — <repo>" --spawn worktree --permission-mode auto` via the Phase 1 auth wrapper (stdout → `/tmp/claude-rc-<persona>.out`)
6. Best-effort scrapes the RC URL from that file (15s timeout); session runs regardless
7. Seeds kickoff prompt via `tmux send-keys` (runbook + item id)
8. Returns the RC URL (or instructs to connect via claude.ai/code)

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
