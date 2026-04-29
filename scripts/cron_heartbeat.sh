#!/bin/bash
# Gmail heartbeat — runs every 10 min during work hours via cron.
# Posts to Discord only if IMMEDIATE priority emails are found.

PROJECT=/mnt/c/Users/aaron/Documents/python/project-jarvis
mkdir -p "$PROJECT/logs"

cd "$PROJECT" || exit 1
source skills/gmail-cleanup/.venv/bin/activate

OUTPUT=$(python skills/gmail-cleanup/skill.py heartbeat 2>>"$PROJECT/logs/cron.log")

if echo "$OUTPUT" | grep -q "^IMMEDIATE:"; then
    echo "$OUTPUT" | python3 scripts/discord_post.py
fi
