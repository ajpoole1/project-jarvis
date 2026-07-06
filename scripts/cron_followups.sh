#!/bin/bash
# Follow-up heartbeat — runs every 10 min during waking hours.
# Reaps expired watches, then fires one proactive outreach if all gates pass.
# Also dispatches any approved scheduled jobs that are due.
# Posts to Discord only when fire produces output. Silent otherwise.

PROJECT=/opt/jarvis-live
mkdir -p "$PROJECT/logs"
cd "$PROJECT" || exit 1

# Belt-and-suspenders: export vars from .jarvis.env without executing the file
# as bash. "source" chokes on values containing <, ;, {, and other shell special
# chars (e.g. Tuya device keys). This while-read loop treats values as literals.
if [ -f "$HOME/.jarvis.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip blank lines and comments
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        # Match KEY=VALUE; strip surrounding single or double quotes from value
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            if [[ "$val" =~ ^\'(.*)\'$ ]] || [[ "$val" =~ ^\"(.*)\"$ ]]; then
                val="${BASH_REMATCH[1]}"
            fi
            export "$key"="$val"
        fi
    done < "$HOME/.jarvis.env"
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
