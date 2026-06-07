# devloop skill

Read-only narration of dev-loop state for the Jarvis dev-crew standup. Part of the Jarvis dev-loop framework (Phase 4).

## Commands

```
python3 skill.py standup    Emit a dev-crew standup summary
```

## Output format

```
**Dev-crew standup (YYYY-MM-DD)**
• Herr Mannkusser: building `2026-0001-foo` — PR #26 (✅ QA pass, awaiting operator merge)
• Authorized, awaiting summon: `2026-0002-bar`
• Recent merges (7d): merge: archive 2026-0000-init (merged)
```

## Data sources (all read-only)

| Source | What |
|---|---|
| `dev-queue` branch | Queue item statuses (proposed/authorized/building/built) |
| `gh pr list` | Open PRs targeting main + QA check status |
| `git log origin/main --merges` | Recent merges (7 days) |
| `knowledge/dev-crew/roster.md` | Persona → repo mapping |

## No venv required

Stdlib-only. Requires `git` and `gh` CLI on PATH. `gh` must be authenticated.

## Morning briefing integration

The morning briefing calls `devloop standup` and includes the output as a block when there is active queue state. See `skills/morning-briefing/skill.py` `_get_dev_crew_standup()`.
