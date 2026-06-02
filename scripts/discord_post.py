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


def _split_chunks(message: str, max_chars: int) -> list[str]:
    """Split at line boundaries so URLs are never cut mid-link."""
    if len(message) <= max_chars:
        return [message]
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for line in message.split("\n"):
        needed = len(line) + (1 if current_lines else 0)  # +1 for the joining newline
        if current_lines and current_len + needed > max_chars:
            chunks.append("\n".join(current_lines))
            current_lines, current_len = [], 0
        current_lines.append(line)
        current_len += needed
    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


def post(message: str) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set in .env", file=sys.stderr)
        sys.exit(1)

    for chunk in _split_chunks(message, MAX_CHARS):
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
