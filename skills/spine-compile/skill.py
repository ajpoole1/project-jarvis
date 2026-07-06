#!/usr/bin/env python3
"""
spine-compile skill — dispatcher wrapper for scripts/spine_compile.py.

Commands:
  run    Daily nonce rotation, MEMORY.md spine block rebuild,
         hook nonce rewrite, lint pass. Posts alerts to Discord on failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path("/opt/jarvis-live/scripts/spine_compile.py")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "run":
        print("Usage: skill.py run", file=sys.stderr)
        return 1

    if not SCRIPT.exists():
        print(f"spine_compile.py not found at {SCRIPT}", file=sys.stderr)
        return 1

    result = subprocess.run(["python3", str(SCRIPT)])
    if result.returncode != 0:
        print(f"spine-compile failed (exit {result.returncode})", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
