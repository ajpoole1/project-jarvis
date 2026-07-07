#!/usr/bin/env python3
"""
spine_check.py — daily sentinel verification (file-based, no gateway dependency).

Checks:
  1. Today's nonce is present in / emitted by all chunk hook scripts.
  2. MEMORY.md final line is the expected tail sentinel for today's nonce.
  3. State file exists and is dated today.
  4. /opt/jarvis-live git tripwire: no uncommitted changes, no divergence from origin/main.

PASS → Discord status ping. FAIL → Discord alert with specifics.

Logs every run to ~/.openclaw/logs/spine-check.log.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────────

STATE_FILE = Path("/home/ajpoole/.openclaw/state/spine-nonce.txt")
LOG_FILE = Path("/home/ajpoole/.openclaw/logs/spine-check.log")
DISCORD_SCRIPT = Path("/opt/jarvis-live/scripts/discord_post.py")

MEMORY_MD = Path(
    "/home/ajpoole/.claude/projects" "/-home-ajpoole--openclaw-workspace/memory/MEMORY.md"
)

HOOK_DIR = Path("/home/ajpoole/.claude/hooks")
# Each entry: (path, chunk_name, check_literal_nonce)
# check_literal_nonce=True  → old pattern: nonce baked as literal in script text
# check_literal_nonce=False → new pattern: hook reads nonce from state file at emission;
#                              spine_check verifies by running the hook and inspecting output
CHUNK_HOOKS = [
    (HOOK_DIR / "jarvis-spine-redlines-core.sh", "REDLINES-CORE", False),
    (HOOK_DIR / "jarvis-spine-redlines-gate.sh", "REDLINES-GATE", False),
    (HOOK_DIR / "jarvis-spine-redlines-untrusted.sh", "REDLINES-UNTRUSTED", True),
    (HOOK_DIR / "jarvis-spine-routing-a.sh", "ROUTING-A", True),
    (HOOK_DIR / "jarvis-spine-routing-b.sh", "ROUTING-B", True),
]


# ── logging ────────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} spine-check: {msg}\n"
    LOG_FILE.open("a").write(line)
    print(msg, file=sys.stderr)


def _post(msg: str) -> None:
    if DISCORD_SCRIPT.exists():
        try:
            subprocess.run(
                ["python3", str(DISCORD_SCRIPT)],
                input=msg,
                text=True,
                capture_output=True,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"discord post failed: {exc}")


def _alert(msg: str) -> None:
    _log(f"ALERT: {msg}")
    _post(f"⚠️ spine-check FAIL: {msg}")


# ── checks ─────────────────────────────────────────────────────────────────────


def check_hooks(nonce: str) -> list[str]:
    """Return a list of failure strings (empty = all pass).

    Hooks that bake the nonce as a literal are checked by text scan.
    Hooks that read the nonce from the state file at emission are checked
    by running the hook and verifying the output contains today's nonce.
    """
    import subprocess as sp

    failures = []
    for hook_path, chunk_name, check_literal in CHUNK_HOOKS:
        if not hook_path.exists():
            failures.append(f"{chunk_name}: hook file missing")
            continue
        expected = f"nonce-{nonce}"
        if check_literal:
            text = hook_path.read_text(encoding="utf-8")
            if expected not in text:
                failures.append(f"{chunk_name}: nonce-{nonce} not found in hook")
            else:
                _log(f"hook OK: {chunk_name} contains nonce-{nonce}")
        else:
            try:
                result = sp.run(
                    ["bash", str(hook_path)],
                    input='{"cwd":"/home/ajpoole/.openclaw/workspace"}',
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if expected not in result.stdout:
                    failures.append(
                        f"{chunk_name}: hook emission does not contain nonce-{nonce} "
                        f"(got: {result.stdout[:80]!r})"
                    )
                else:
                    _log(f"hook OK: {chunk_name} emitted nonce-{nonce}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{chunk_name}: hook execution failed: {exc}")
    return failures


OPT_LIVE = Path("/opt/jarvis-live")


def check_opt_tripwire() -> list[str]:
    """Return failure strings if /opt/jarvis-live has drift from origin/main."""
    failures = []
    if not OPT_LIVE.exists():
        return ["/opt/jarvis-live missing"]
    try:
        dirty = subprocess.run(
            ["git", "-C", str(OPT_LIVE), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if dirty.stdout.strip():
            failures.append(
                f"/opt/jarvis-live has uncommitted changes: {dirty.stdout.strip()[:120]}"
            )
        else:
            _log("tripwire OK: /opt/jarvis-live working tree clean")

        diverge = subprocess.run(
            ["git", "-C", str(OPT_LIVE), "rev-list", "--count", "HEAD...origin/main"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        count = diverge.stdout.strip()
        if count != "0":
            failures.append(f"/opt/jarvis-live diverged from origin/main by {count} commit(s)")
        else:
            _log("tripwire OK: /opt/jarvis-live is in sync with origin/main")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"/opt/jarvis-live git check failed: {exc}")
    return failures


def check_memory_tail(nonce: str) -> list[str]:
    """Return a list of failure strings (empty = pass)."""
    if not MEMORY_MD.exists():
        return [f"MEMORY.md not found: {MEMORY_MD}"]
    lines = MEMORY_MD.read_text(encoding="utf-8").rstrip("\n").split("\n")
    final_line = lines[-1] if lines else ""
    expected = f"[MEMORY TAIL SENTINEL nonce-{nonce}]"
    if final_line != expected:
        return [f"MEMORY.md tail mismatch — expected: {expected!r} got: {final_line!r}"]
    _log(f"MEMORY.md tail OK: {expected}")
    return []


# ── main ───────────────────────────────────────────────────────────────────────


def main() -> int:
    _log("=== spine-check start ===")

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if not STATE_FILE.exists():
        _alert("state file missing — spine_compile.py may not have run today")
        return 1

    lines = STATE_FILE.read_text().splitlines()
    if not lines or not lines[0].startswith(today):
        _alert(f"state file stale — no nonce for {today}")
        return 1

    parts = lines[0].split()
    if len(parts) < 2:
        _alert(f"state file malformed: {lines[0]!r}")
        return 1

    nonce = parts[1]
    _log(f"checking nonce: {nonce}")

    failures = check_hooks(nonce) + check_memory_tail(nonce) + check_opt_tripwire()

    if failures:
        _alert("; ".join(failures))
        return 1

    n = len(CHUNK_HOOKS)
    msg = f"✅ spine-check PASS nonce-{nonce}: all {n} hooks + MEMORY tail OK"
    _log(msg)
    _post(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
