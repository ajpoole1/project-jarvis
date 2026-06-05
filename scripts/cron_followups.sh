#!/bin/bash
# Follow-up heartbeat — runs every 10 min during waking hours.
# Reaps expired watches, then fires one proactive outreach if all gates pass.
# Also dispatches any approved scheduled jobs that are due.
# Posts to Discord only when fire produces output. Silent otherwise.

PROJECT=/mnt/c/Users/aaron/Documents/python/project-jarvis
mkdir -p "$PROJECT/logs"
cd "$PROJECT" || exit 1

# Belt-and-suspenders: load .jarvis.env so JARVIS_DATA_DIR is set even if cron
# didn't inherit it. The skills also self-load, so this is defense in depth.
if [ -f "$HOME/.jarvis.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$HOME/.jarvis.env"
    set +a
fi

# Reap expired window_until watches — no output unless something lapsed
python3 "$PROJECT/skills/followups/skill.py" reap 2>>"$PROJECT/logs/cron.log"

# Dispatch any approved scheduled jobs that are due
python3 "$PROJECT/skills/schedules/skill.py" dispatch 2>>"$PROJECT/logs/cron.log"

# Evaluate gates + fire
OUTPUT=$(python3 "$PROJECT/skills/followups/skill.py" fire 2>>"$PROJECT/logs/cron.log")

if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 scripts/discord_post.py
fi
