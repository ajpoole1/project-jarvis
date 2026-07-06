#!/usr/bin/env python3
"""
spine-check skill — dispatcher wrapper for scripts/spine_check.py.

Commands:
  run    Verify all 4 hook scripts and MEMORY.md tail carry today's nonce.
         Check /opt/jarvis-live for drift from origin/main.
         Posts PASS/FAIL to Discord.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path("/opt/jarvis-live/scripts/spine_check.py")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "run":
        print("Usage: skill.py run", file=sys.stderr)
        return 1

    if not SCRIPT.exists():
        print(f"spine_check.py not found at {SCRIPT}", file=sys.stderr)
        return 1

    result = subprocess.run(["python3", str(SCRIPT)], capture_output=True, text=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        print(f"spine-check failed (exit {result.returncode})", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
