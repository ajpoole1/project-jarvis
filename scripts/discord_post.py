#!/usr/bin/env python3
"""Post a message to Discord via webhook. Reads from stdin. Chunks at Discord's 2000-char limit."""

import json
import os
import sys
import urllib.request
from pathlib import Path

# Manual .env parse — stdlib only, no dotenv dependency
_env_path = Path(__file__).parents[1] / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MAX_CHARS = 1900  # Discord limit is 2000; leave buffer for safety


def post(message: str) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set in .env", file=sys.stderr)
        sys.exit(1)

    chunks = [message[i : i + MAX_CHARS] for i in range(0, len(message), MAX_CHARS)]
    for chunk in chunks:
        data = json.dumps({"content": chunk}).encode()
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/project-jarvis, 1.0)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                print(f"Discord webhook returned {resp.status}", file=sys.stderr)
                sys.exit(1)


if __name__ == "__main__":
    message = sys.stdin.read().strip()
    if message:
        post(message)
