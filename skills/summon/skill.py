#!/usr/bin/env python3
"""
summon skill — launch and manage named builder sessions via claude remote-control.

Commands:
  summon <persona> <id>   Launch a named RC session for an authorized item
  dismiss <persona>       Kill the tmux session for a persona
  reaper                  Kill all crew tmux sessions idle past IDLE_MINUTES
  status                  List live crew sessions with idle times

Requirements:
  - tmux installed
  - claude CLI v2.1.52+ (remote-control support); full subcommand set v2.1.79+
  - scripts/dev-crew/launch.sh (Phase 1 auth wrapper)
  - knowledge/dev-crew/roster.md (persona definitions)

Security:
  - Only the operator may summon; never called automatically on authorize.
  - Validated argv: persona must match a roster entry; id format checked.
  - Concurrency guard: warns when live builder count exceeds threshold.
  - Auth assertion is delegated to launch.sh (aborts + alerts on mismatch).
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
ROSTER_FILE = PROJECT / "knowledge/dev-crew/roster.md"
LAUNCH_SH = PROJECT / "scripts/dev-crew/launch.sh"
RUNBOOK = "docs/dev-loop/BUILD_RUNBOOK.md"
DEVNOTES_QUEUE = "knowledge/dev-notes/queue"

# tmux session names are prefixed so we can identify crew sessions
TMUX_PREFIX = "crew-"

# Minimum claude version for remote-control
RC_MIN_VERSION = (2, 1, 52)

# Warn if this many builder sessions are live simultaneously
CONCURRENCY_WARN_THRESHOLD = 2

# Reaper kills sessions idle longer than this
IDLE_MINUTES = 60


# ── roster parser ─────────────────────────────────────────────────────────────


def load_roster() -> dict[str, dict]:
    """Parse roster.md; return {id: fields}."""
    if not ROSTER_FILE.exists():
        return {}
    text = ROSTER_FILE.read_text(encoding="utf-8")
    personas: dict[str, dict] = {}
    # Each persona block: ## Heading followed by ```yaml ... ```
    for block in re.finditer(r"##\s+(.+?)\n+```yaml\n(.*?)```", text, re.DOTALL):
        raw = block.group(2)
        fields: dict = {}
        for line in raw.splitlines():
            # simple key: value (skip list items for now)
            m = re.match(r"^(\w+):\s*(.+)", line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
        if "id" in fields:
            personas[fields["id"]] = fields
    return personas


def get_persona(persona_id: str) -> dict:
    roster = load_roster()
    if not roster:
        raise SystemExit(f"Roster not found at {ROSTER_FILE}")
    p = roster.get(persona_id)
    if not p:
        available = ", ".join(roster.keys()) or "(none)"
        raise SystemExit(f"Persona '{persona_id}' not in roster. Available: {available}")
    for req in ("name", "repo_dir", "auth", "permission_mode"):
        if req not in p:
            raise SystemExit(f"Persona '{persona_id}' missing required field '{req}' in roster")
    return p


# ── version check ─────────────────────────────────────────────────────────────


def check_claude_version() -> None:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        raw = out.stdout.strip()
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
        if not m:
            print(
                f"Warning: could not parse claude version from '{raw}' — proceeding",
                file=sys.stderr,
            )
            return
        actual = tuple(int(x) for x in m.groups())
        if actual < RC_MIN_VERSION:
            raise SystemExit(
                f"claude v{'.'.join(str(x) for x in actual)} < required "
                f"v{'.'.join(str(x) for x in RC_MIN_VERSION)} for remote-control"
            )
    except FileNotFoundError:
        raise SystemExit("claude CLI not found on PATH") from None


# ── tmux helpers ──────────────────────────────────────────────────────────────


def session_name(persona_id: str) -> str:
    return f"{TMUX_PREFIX}{persona_id}"


def list_crew_sessions() -> list[str]:
    """Return list of live crew tmux session names."""
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        return [s for s in out.stdout.splitlines() if s.startswith(TMUX_PREFIX)]
    except FileNotFoundError:
        raise SystemExit("tmux not found — install tmux to use summon") from None


def session_idle_seconds(sname: str) -> int:
    """Return seconds since last activity in the session's active pane."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-t", sname, "-p", "#{pane_last_used}"],
            capture_output=True,
            text=True,
        )
        last_used = int(out.stdout.strip() or "0")
        return max(0, int(time.time()) - last_used)
    except (ValueError, subprocess.SubprocessError):
        return 0


def kill_session(sname: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", sname], capture_output=True)


def send_keys(sname: str, text: str, *, enter: bool = True) -> None:
    keys = [text, "Enter"] if enter else [text]
    subprocess.run(["tmux", "send-keys", "-t", sname, *keys], capture_output=True)


# ── URL capture ───────────────────────────────────────────────────────────────


def capture_rc_url(url_file: Path, timeout: int = 15) -> str | None:
    """Poll url_file for the RC URL printed by claude remote-control. Best-effort."""
    deadline = time.time() + timeout
    url_pattern = re.compile(r"https://claude\.ai/code[^\s\"']+")
    while time.time() < deadline:
        if url_file.exists():
            content = url_file.read_text(errors="ignore")
            m = url_pattern.search(content)
            if m:
                return m.group(0)
        time.sleep(0.5)
    return None


# ── concurrency guard ─────────────────────────────────────────────────────────


def concurrency_check() -> None:
    live = list_crew_sessions()
    if len(live) >= CONCURRENCY_WARN_THRESHOLD:
        names = ", ".join(live)
        print(
            f"Warning: {len(live)} builder session(s) already live ({names}). "
            "Proceeding — dismiss idle sessions with: summon dismiss <persona>",
            file=sys.stderr,
        )


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_summon(args: list[str]) -> int:
    if len(args) < 2:
        print("Usage: summon <persona> <id>", file=sys.stderr)
        return 1

    persona_id, item_id = args[0], args[1]

    # Validate item id format (YYYY-NNNN-slug)
    if not re.match(r"^\d{4}-\d{4}-.+", item_id):
        print(
            f"Warning: item id '{item_id}' doesn't match expected format YYYY-NNNN-slug",
            file=sys.stderr,
        )

    check_claude_version()
    persona = get_persona(persona_id)
    concurrency_check()

    sname = session_name(persona_id)
    live = list_crew_sessions()
    if sname in live:
        print(
            f"Session '{sname}' already running. Dismiss it first: summon dismiss {persona_id}",
            file=sys.stderr,
        )
        return 1

    repo_dir = persona["repo_dir"]
    auth_profile = persona["role"]
    permission_mode = persona.get("permission_mode", "auto")
    rc_spawn = persona.get("rc_spawn", "worktree")
    display_name = f"{persona['name']} — {Path(repo_dir).name}"

    url_file = Path(f"/tmp/claude-rc-{persona_id}.out")
    url_file.unlink(missing_ok=True)

    # Build the RC launch command via the Phase 1 auth wrapper.
    # Stdout redirected to url_file so we can scrape the URL.
    # The wrapper calls: env -i ... claude remote-control --name "..." ...
    rc_cmd = (
        f"{LAUNCH_SH} {auth_profile} -- "
        f"remote-control "
        f'--name "{display_name}" '
        f"--spawn {rc_spawn} "
        f"--permission-mode {permission_mode} "
        f"> {url_file} 2>&1"
    )

    print(f"Launching {persona['name']} in {repo_dir} ...")
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", sname, "-c", repo_dir, rc_cmd],
        check=True,
    )

    # Best-effort URL capture
    url = capture_rc_url(url_file, timeout=15)
    if url:
        print(f"RC session URL: {url}")
    else:
        print(
            "Warning: RC URL not captured within timeout — session is running; connect via claude.ai/code",
            file=sys.stderr,
        )

    # Seed kickoff prompt after a short delay for the session to initialize
    time.sleep(3)
    spec_path = f"{DEVNOTES_QUEUE}/{item_id}.md"
    kickoff = (
        f"You are {persona['name']}, a Jarvis dev-loop builder. "
        f"Load {RUNBOOK} then run: scripts/dev-loop/start-build.sh {item_id} "
        f"The spec is at {spec_path} on the dev-queue branch. "
        f"Follow the runbook exactly. Stop and ask via Discord on any ambiguity."
    )
    send_keys(sname, kickoff)
    print(f"Kickoff prompt seeded to session '{sname}'.")

    result_url = url or "(connect via claude.ai/code)"
    print(f"\nSession '{sname}' live. Link: {result_url}")
    return 0


def cmd_dismiss(args: list[str]) -> int:
    if not args:
        print("Usage: dismiss <persona>", file=sys.stderr)
        return 1
    persona_id = args[0]
    sname = session_name(persona_id)
    live = list_crew_sessions()
    if sname not in live:
        print(f"No live session for '{persona_id}'")
        return 0
    kill_session(sname)
    print(f"Dismissed session '{sname}'")
    return 0


def cmd_reaper(_args: list[str]) -> int:
    live = list_crew_sessions()
    if not live:
        return 0
    reaped = []
    for sname in live:
        idle = session_idle_seconds(sname)
        if idle >= IDLE_MINUTES * 60:
            kill_session(sname)
            reaped.append(f"{sname} (idle {idle // 60}m)")
    if reaped:
        print(f"Reaped: {', '.join(reaped)}")
    return 0


def cmd_status(_args: list[str]) -> int:
    live = list_crew_sessions()
    if not live:
        print("No live crew sessions")
        return 0
    print(f"Live crew sessions ({len(live)}):")
    for sname in live:
        idle = session_idle_seconds(sname)
        persona_id = sname.removeprefix(TMUX_PREFIX)
        print(f"  {sname}  idle={idle // 60}m  persona={persona_id}")
    return 0


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: skill.py <summon|dismiss|reaper|status> [args...]", file=sys.stderr)
        return 1

    cmd, *rest = sys.argv[1:]
    dispatch = {
        "summon": cmd_summon,
        "dismiss": cmd_dismiss,
        "reaper": cmd_reaper,
        "status": cmd_status,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(f"Unknown command '{cmd}'; use summon|dismiss|reaper|status", file=sys.stderr)
        return 1
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
