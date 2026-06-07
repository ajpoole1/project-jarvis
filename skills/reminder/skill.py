"""
Reminder skill — print a free-text message to stdout.

The schedules dispatcher forwards any non-empty stdout to Discord, so printing
IS posting. Do NOT post to Discord directly from this skill.

Usage:
  skill.py --message "text" [--prefix "⏰ Reminder"]
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a reminder message to stdout.")
    parser.add_argument("--message", required=True, help="Reminder text to print")
    parser.add_argument(
        "--prefix", default="⏰ Reminder", help="Optional prefix prepended to the message"
    )
    args = parser.parse_args()

    message = args.message.strip()
    if not message:
        print("Error: --message cannot be empty", file=sys.stderr)
        sys.exit(1)

    prefix = args.prefix.strip()
    output = f"{prefix} {message}" if prefix else message
    print(output)


if __name__ == "__main__":
    main()
