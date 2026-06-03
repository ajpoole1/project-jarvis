"""Garden skill — care reminders, observation logging, and almanac queries."""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path.home() / ".jarvis.env")

ALMANAC = Path(__file__).parent.parent.parent / "knowledge" / "garden" / "almanac.md"

# (keyword, month_number) — ordered longest-first to avoid substring false matches
_MONTH_KEYWORDS: list[tuple[str, int]] = [
    ("january", 1),
    ("february", 2),
    ("september", 9),
    ("november", 11),
    ("december", 12),
    ("october", 10),
    ("august", 8),
    ("april", 4),
    ("march", 3),
    ("june", 6),
    ("july", 7),
    ("sept", 9),
    ("jan", 1),
    ("feb", 2),
    ("mar", 3),
    ("apr", 4),
    ("may", 5),
    ("jun", 6),
    ("jul", 7),
    ("aug", 8),
    ("sep", 9),
    ("oct", 10),
    ("nov", 11),
    ("dec", 12),
]

_MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

# zone alias → prefix of the ## heading that owns its almanac log
_ZONE_PREFIX: dict[str, str] = {
    "1": "## Zone 1",
    "2": "## Zone 2",
    "3": "## Zone 3",
    "3a": "## Zone 3",
    "3b": "## Zone 3",
    "3c": "## Zone 3",
    "4": "## Zone 4",
    "5": "## Zone 5",
    "6": "## Zone 6",
    "7": "## Zone 7",
    "d": "## Zone D",
    "deck": "## Zone D",
}


def _parse_month(s: str) -> int | None:
    s = s.lower().strip()
    try:
        n = int(s)
        if 1 <= n <= 12:
            return n
    except ValueError:
        pass
    for keyword, num in _MONTH_KEYWORDS:
        if s == keyword:
            return num
    return None


def cmd_reminders(month_arg: str = "") -> str:
    month = _parse_month(month_arg) if month_arg else date.today().month
    if month is None:
        return f"Unknown month: {month_arg}"

    text = ALMANAC.read_text(encoding="utf-8")
    table_match = re.search(r"### Key Seasonal Reminders\n(.+?)(?=\n###|\n##|\Z)", text, re.DOTALL)
    if not table_match:
        return "Seasonal reminders table not found in almanac."

    rows: list[tuple[str, str]] = []
    for line in table_match.group(1).splitlines():
        if not line.startswith("|") or "---" in line or "When" in line:
            continue
        parts = [p.strip() for p in line.strip("|").split("|", 1)]
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))

    matching = []
    for when, what in rows:
        when_lower = when.lower()
        for keyword, num in _MONTH_KEYWORDS:
            if keyword in when_lower and num == month:
                matching.append((when, what))
                break

    month_name = _MONTH_NAMES[month]
    if not matching:
        return f"No specific reminders for {month_name}."

    lines = [f"**Garden reminders — {month_name}**"]
    for when, what in matching:
        lines.append(f"  **{when}:** {what}")
    return "\n".join(lines)


def cmd_log(zone: str, note: str) -> str:
    zone_key = zone.lower().strip()
    heading_prefix = _ZONE_PREFIX.get(zone_key)
    if heading_prefix is None:
        return f"Unknown zone: '{zone}'. Valid zones: 1, 2, 3, 3a, 3b, 3c, 4, 5, 6, 7, d"

    # escape pipes in the note so they don't break the markdown table
    safe_note = note.replace("|", r"\|")

    text = ALMANAC.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # find the zone heading line
    zone_start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(heading_prefix):
            zone_start = i
            break
    if zone_start is None:
        return f"Zone heading '{heading_prefix}' not found in almanac."

    # find ### Almanac Log within this zone (stop at the next ## heading)
    log_header_idx: int | None = None
    for i in range(zone_start + 1, len(lines)):
        stripped = lines[i].rstrip()
        if stripped.startswith("## ") and i != zone_start:
            break
        if stripped == "### Almanac Log":
            log_header_idx = i
            break
    if log_header_idx is None:
        return f"No almanac log found for zone {zone}."

    # find the table separator row |---|---|
    sep_idx: int | None = None
    for i in range(log_header_idx + 1, min(log_header_idx + 6, len(lines))):
        if re.match(r"\|[-| ]+\|", lines[i]):
            sep_idx = i
            break
    if sep_idx is None:
        return "Almanac log table structure not found."

    today_str = date.today().strftime("%b %-d, %Y")
    lines.insert(sep_idx + 1, f"| {today_str} | {safe_note} |\n")
    ALMANAC.write_text("".join(lines), encoding="utf-8")
    return f"Logged to Zone {zone.upper()}: **{today_str}** — {note}"


def cmd_ask(question: str) -> str:
    almanac_text = ALMANAC.read_text(encoding="utf-8")
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=(
            "You are a gardening assistant for a Zone 4–5 Québec garden. "
            "Answer concisely using only the almanac provided. "
            "If the answer isn't in the almanac, say so briefly."
        ),
        messages=[
            {
                "role": "user",
                "content": f"ALMANAC:\n{almanac_text}\n\nQUESTION: {question}",
            }
        ],
    )
    return response.content[0].text


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "reminders"

    if cmd == "reminders":
        month_arg = sys.argv[2] if len(sys.argv) > 2 else ""
        print(cmd_reminders(month_arg))
    elif cmd == "log":
        if len(sys.argv) < 4:
            print("Usage: skill.py log <zone> <note>")
            sys.exit(1)
        print(cmd_log(sys.argv[2], sys.argv[3]))
    elif cmd == "ask":
        if len(sys.argv) < 3:
            print("Usage: skill.py ask <question>")
            sys.exit(1)
        print(cmd_ask(" ".join(sys.argv[2:])))
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: reminders [month], log <zone> <note>, ask <question>")
        sys.exit(1)
