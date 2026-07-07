---
name: handoff
description: Scaffold and append a session entry to CONTEXT.md at the end of a work session. Invoke at the end of every session, or when /ship closes out.
---

# /handoff — session entry → CONTEXT.md

Append a session entry to `CONTEXT.md`. Keep it tight — this is a structured scaffold, not an essay.

## Procedure

**Step 1 — Read current CONTEXT.md**
Read the file to understand its existing structure and find the correct insertion point (top of the session log, or bottom — follow the established pattern).

**Step 2 — Collect session state**
From this session, assemble:
- **Completed:** specific files created/edited/deleted, with paths. Function names if relevant.
- **In progress:** what was started but not finished, and exactly where it was left.
- **Blocked:** anything halted, and why.
- **Next:** the immediate next action for whoever picks this up.

**Step 3 — Write the entry**
Append (do not overwrite) a dated entry following the existing format in CONTEXT.md.
If no entries exist yet, establish the format:

```
## Session — YYYY-MM-DD

**Completed:**
- ...

**In progress:**
- ...

**Blocked:**
- ...

**Next:**
- ...
```

One entry per session. Date is today's date. Be specific about file paths; skip anything that is obvious from the git diff.
