#!/bin/bash
# Gmail digest — runs at 13:00 and 18:00 via cron.
# Posts to Discord unless queue is empty.

PROJECT=/mnt/c/Users/aaron/Documents/python/project-jarvis
mkdir -p "$PROJECT/logs"

cd "$PROJECT" || exit 1
source skills/gmail-cleanup/.venv/bin/activate

OUTPUT=$(python skills/gmail-cleanup/skill.py digest 2>>"$PROJECT/logs/cron.log")

if [ -n "$OUTPUT" ] && [ "$OUTPUT" != "DIGEST EMPTY" ]; then
    echo "$OUTPUT" | python3 scripts/discord_post.py
fi
