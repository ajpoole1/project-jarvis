"""Tests for skills/reminder/skill.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SKILL = Path(__file__).parents[1] / "skills" / "reminder" / "skill.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SKILL)] + list(args),
        capture_output=True,
        text=True,
    )


def test_prints_message_with_default_prefix():
    result = _run("--message", "Buy groceries")
    assert result.returncode == 0
    assert result.stdout.strip() == "⏰ Reminder Buy groceries"


def test_prints_message_with_custom_prefix():
    result = _run("--message", "Check the oven", "--prefix", "🔥")
    assert result.returncode == 0
    assert result.stdout.strip() == "🔥 Check the oven"


def test_empty_message_exits_nonzero():
    result = _run("--message", "")
    assert result.returncode != 0
    assert result.stderr.strip() != ""


def test_missing_message_exits_nonzero():
    result = _run()
    assert result.returncode != 0
