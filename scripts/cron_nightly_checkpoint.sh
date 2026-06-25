#!/bin/bash
# Nightly checkpoint-restart — 2026-0042.
# Runs at 04:00 via WSL cron. Capture → restart → verify.
# See scripts/nightly_checkpoint.py for the full implementation.
#
# Crontab line (add with: crontab -e):
#   0 4 * * * /mnt/c/Users/aaron/Documents/python/project-jarvis/scripts/cron_nightly_checkpoint.sh

PROJECT=/mnt/c/Users/aaron/Documents/python/project-jarvis
mkdir -p "$PROJECT/logs"

# Load env vars (same safe parser as cron_followups.sh)
if [ -f "$HOME/.jarvis.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
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

python3 "$PROJECT/scripts/nightly_checkpoint.py" >> "$PROJECT/logs/nightly_checkpoint.log" 2>&1
