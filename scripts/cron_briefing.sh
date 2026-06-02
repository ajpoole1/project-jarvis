#!/bin/bash
# Morning briefing — runs at 7:00am via cron. Posts 3 messages to Discord.

PROJECT=/mnt/c/Users/aaron/Documents/python/project-jarvis
mkdir -p "$PROJECT/logs"

# Atomic lock: only one instance can run at a time; second fire exits immediately
# Lock must be on native Linux tmpfs — flock is unreliable on /mnt/c/ (DrvFs)
LOCKFILE="/tmp/jarvis-briefing.lock"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "$(date): briefing skipped — already running or ran recently" >> "$PROJECT/logs/cron.log"
    exit 0
fi

# Secondary dedup: skip if already ran within the last 30 minutes (catches staggered double-fires)
STAMP="/tmp/jarvis-briefing-last-run"
if [ -f "$STAMP" ]; then
    AGE=$(( $(date +%s) - $(date -r "$STAMP" +%s) ))
    if [ "$AGE" -lt 1800 ]; then
        echo "$(date): briefing skipped — already ran ${AGE}s ago" >> "$PROJECT/logs/cron.log"
        exit 0
    fi
fi
touch "$STAMP"

cd "$PROJECT" || exit 1
source skills/morning-briefing/.venv/bin/activate

TMPFILE=$(mktemp /tmp/jarvis-briefing.XXXXXX)
python skills/morning-briefing/skill.py > "$TMPFILE" 2>>"$PROJECT/logs/cron.log"

if [ -s "$TMPFILE" ]; then
    python3 - "$TMPFILE" <<'PYEOF'
import sys, subprocess
content = open(sys.argv[1]).read()
parts = [p.strip() for p in content.split("---SPLIT---") if p.strip()]
for part in parts:
    subprocess.run(["python3", "scripts/discord_post.py"], input=part, text=True)
PYEOF
fi

rm -f "$TMPFILE"
