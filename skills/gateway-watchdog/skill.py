#!/usr/bin/env python3
"""
gateway-watchdog — samples openclaw-gateway RSS and alerts to Discord if it
exceeds a threshold, converting a silent overnight OOM into an early warning.

Called by the schedules dispatcher on a 10m interval (same as heartbeat).
No venv needed: stdlib only.

Commands:
  check                     Sample RSS, alert if above threshold (default)

Environment (from ~/.jarvis.env):
  JARVIS_DATA_DIR           Path to jarvis.db (default: /data)
  GATEWAY_RSS_WARN_MB       Alert threshold in MB (default: 1500)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).parents[2]
_DISCORD_SCRIPT = _SKILL_ROOT / "scripts" / "discord_post.py"

# Load ~/.jarvis.env
_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

_WARN_THRESHOLD_MB = int(os.environ.get("GATEWAY_RSS_WARN_MB", "1500"))


def _gateway_rss_mb() -> int | None:
    """Return openclaw-gateway RSS in MB, or None if not found."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "rss,comm"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and "openclaw-gatewa" in parts[1]:
                return int(parts[0]) // 1024
    except Exception:
        pass
    return None


def _post_discord(message: str) -> None:
    if not _DISCORD_SCRIPT.exists():
        print(message)
        return
    try:
        subprocess.run(
            ["python3", str(_DISCORD_SCRIPT)],
            input=message,
            text=True,
            timeout=15,
            capture_output=True,
        )
    except Exception:
        print(message)


def cmd_check() -> None:
    rss_mb = _gateway_rss_mb()

    if rss_mb is None:
        # Gateway not running — the heartbeat will catch outages separately
        return

    if rss_mb >= _WARN_THRESHOLD_MB:
        msg = (
            f"⚠️ **Gateway memory warning:** openclaw-gateway RSS is **{rss_mb} MB** "
            f"(threshold: {_WARN_THRESHOLD_MB} MB). "
            f"Approaching OOM — consider restarting: `systemctl --user restart openclaw-gateway`"
        )
        _post_discord(msg)


def main() -> None:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "check"
    if cmd == "check":
        cmd_check()
    else:
        print(f"Unknown command: {cmd!r}. Use: check", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
