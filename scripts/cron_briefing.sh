#!/bin/bash
# Morning briefing — runs at 7:00am via cron. Posts 3 messages to Discord.

PROJECT=/mnt/c/Users/aaron/Documents/python/project-jarvis
mkdir -p "$PROJECT/logs"

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
