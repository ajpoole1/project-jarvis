#!/bin/bash
# Morning briefing — runs at 7:00am via cron. Always posts to Discord.

PROJECT=/mnt/c/Users/aaron/Documents/python/project-jarvis
mkdir -p "$PROJECT/logs"

cd "$PROJECT" || exit 1
source skills/morning-briefing/.venv/bin/activate

OUTPUT=$(python skills/morning-briefing/skill.py 2>>"$PROJECT/logs/cron.log")

if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 scripts/discord_post.py
fi
