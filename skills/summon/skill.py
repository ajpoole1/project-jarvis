#!/usr/bin/env python3
"""
summon skill — launch and manage named builder sessions via claude remote-control.

Commands:
  summon <persona> <id>     Launch a named RC builder for an authorized item
  ask <id> <question...>    (builder) post a blocking question to the dev-loop Discord
  watchdog-check <id>       (scheduler) one-shot stall check; prints alert if stalled
  dismiss <persona>         Kill the tmux session + worktree for a persona
  reaper                    Kill all crew tmux sessions idle past IDLE_MINUTES
  status                    List live crew sessions with idle times

Kickoff model (DEV_LOOP_REFERENCE §8 / BUILD_RUNBOOK):
  The kickoff is delivered as a positional prompt to an interactive Remote-Control
  session — `claude --remote-control "<name>" --permission-mode <mode> "<kickoff>"` —
  which auto-submits it on startup. The builder runs in a dedicated git WORKTREE so it
  never disturbs the operator's checkout; the worktree inherits the repo's trust, so no
  trust dialog blocks the unattended start. There is no `tmux send-keys` to a server
  console: the previous send-keys path raced the session and is the bug this replaces.

Surfacing model (Part 3b/3c):
  - A builder that hits ambiguity runs `ask`, which posts the question to the MAIN
    Discord channel (scripts/discord_post.py → DISCORD_WEBHOOK_URL) so it reaches AJ
    wherever he is, and records that the run is blocked.
  - At summon time a once@ schedule is armed (reusing the schedules skill). N minutes
    later the heartbeat dispatcher runs `watchdog-check`; if the build has neither
    opened a PR/pushed its branch nor posted a question, the check prints a stall alert
    that the dispatcher forwards to Discord. The one-off then retires itself.

Requirements:
  - tmux installed
  - claude CLI v2.1.52+ (remote-control support)
  - scripts/dev-crew/launch.sh (Phase 1 auth wrapper; pins the builder model)
  - knowledge/dev-crew/roster.md (persona definitions)

Security:
  - Only the operator may summon; never called automatically on authorize.
  - Validated argv: persona must match a roster entry; id format checked.
  - Concurrency guard: warns when live builder count exceeds threshold.
  - Auth assertion is delegated to launch.sh (aborts + alerts on mismatch).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[2]
ROSTER_FILE = PROJECT / "knowledge/dev-crew/roster.md"
LAUNCH_SH = PROJECT / "scripts/dev-crew/launch.sh"
DISCORD_SCRIPT = PROJECT / "scripts/discord_post.py"
SCHEDULES_SKILL = PROJECT / "skills/schedules/skill.py"
RUNBOOK = "docs/dev-loop/BUILD_RUNBOOK.md"
DEVNOTES_QUEUE = "knowledge/dev-notes/queue"

# Load ~/.jarvis.env so JARVIS_DATA_DIR / DISCORD_WEBHOOK_URL resolve whether this skill
# is called by the operator, by a builder session, or by the heartbeat dispatcher.
_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# tmux session names are prefixed so we can identify crew sessions
TMUX_PREFIX = "crew-"

# Minimum claude version for remote-control
RC_MIN_VERSION = (2, 1, 52)

# Warn if this many builder sessions are live simultaneously
CONCURRENCY_WARN_THRESHOLD = 2

# Reaper kills sessions idle longer than this
IDLE_MINUTES = 60

# Watchdog: minutes after a summon to run the one-shot stall check.
WATCHDOG_MINUTES = 30

_LOCAL_TZ = ZoneInfo("America/Toronto")


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
    for req in ("name", "repo_dir", "role", "permission_mode"):
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


# ── URL capture ───────────────────────────────────────────────────────────────


def capture_rc_url(url_file: Path, timeout: int = 15) -> str | None:
    """Poll url_file for the RC URL printed by claude. Best-effort."""
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


# ── worktree + kickoff (pure helpers, unit-tested) ────────────────────────────


def worktree_path(repo_dir: str, item_id: str) -> Path:
    """Dedicated build worktree for an item — a sibling of the repo, never nested in it."""
    return Path(repo_dir).resolve().parent / f"jarvis-build-{item_id}"


def build_kickoff(persona_name: str, item_id: str) -> str:
    """Single-line kickoff prompt auto-submitted into the RC session.

    Kept to one line: it is passed as a positional CLI argument, shell-quoted by the
    caller. No embedded newlines.
    """
    spec_path = f"{DEVNOTES_QUEUE}/{item_id}.md"
    return (
        f"You are {persona_name}, a Jarvis dev-loop builder. "
        f"Load {RUNBOOK}, then run: scripts/dev-loop/start-build.sh {item_id} "
        f"(the spec is at {spec_path} on the dev-queue branch). "
        f"Follow the runbook exactly. Build on the feature branch, run tests and lint, "
        f"then open a PR with scripts/dev-loop/open-pr.sh {item_id}. "
        f"If the spec is under-specified for a required decision, do NOT guess: post your "
        f'question with `python3 skills/summon/skill.py ask {item_id} "<your question>"` '
        f"(this posts to the dev-loop Discord and records that you are blocked), then stop."
    )


def is_stalled(row: dict, progressed: bool) -> bool:
    """A run is stalled iff there is no progress (PR/branch) and no blocking question."""
    return not progressed and not row.get("question_at")


# ── dev-crew run tracking (SQLite, shared jarvis.db) ──────────────────────────


def _db_path() -> Path:
    return Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"


def _db() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dev_crew_runs (
            item        TEXT PRIMARY KEY,
            persona     TEXT NOT NULL,
            worktree    TEXT NOT NULL DEFAULT '',
            summoned_at TEXT NOT NULL,
            question_at TEXT
        )
    """)
    conn.commit()
    return conn


def _record_summon(item_id: str, persona_id: str, worktree: str, now: datetime) -> None:
    conn = _db()
    try:
        # A fresh summon for an item resets its blocked state.
        conn.execute(
            """INSERT INTO dev_crew_runs (item, persona, worktree, summoned_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(item) DO UPDATE SET
                   persona=excluded.persona,
                   worktree=excluded.worktree,
                   summoned_at=excluded.summoned_at,
                   question_at=NULL""",
            (item_id, persona_id, worktree, now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _set_question(item_id: str, now: datetime) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE dev_crew_runs SET question_at = ? WHERE item = ? AND question_at IS NULL",
            (now.isoformat(), item_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── Discord ───────────────────────────────────────────────────────────────────


def _post_discord(message: str) -> bool:
    """Post to the MAIN Discord channel via scripts/discord_post.py (stdin). Best-effort.

    discord_post.py routes to DISCORD_WEBHOOK_URL — the main channel — deliberately, so a
    blocked build reaches AJ wherever he is. DISCORD_DEVLOOP_WEBHOOK stays for QA verdicts.
    """
    if not DISCORD_SCRIPT.exists():
        print(f"discord_post.py not found at {DISCORD_SCRIPT}", file=sys.stderr)
        return False
    try:
        result = subprocess.run(
            ["python3", str(DISCORD_SCRIPT)],
            input=message,
            text=True,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            print(f"discord_post.py failed: {result.stderr.strip()}", file=sys.stderr)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"discord_post.py error: {exc}", file=sys.stderr)
        return False


# ── progress detection ────────────────────────────────────────────────────────


def _branch_progressed(repo_dir: str, item_id: str) -> bool:
    """True if the builder has opened a PR or pushed its feature branch.

    Tries `gh pr list` first; falls back to a remote-branch check so it still works when
    gh is unauthenticated in the dispatcher environment.
    """
    branch = f"feature/{item_id}"
    try:
        pr = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if pr.returncode == 0:
            data = json.loads(pr.stdout or "[]")
            if isinstance(data, list) and data:
                return True
    except Exception:  # noqa: BLE001
        pass
    try:
        ls = subprocess.run(
            ["git", "-C", repo_dir, "ls-remote", "--heads", "origin", branch],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return ls.returncode == 0 and bool(ls.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


# ── watchdog arming (reuses the schedules skill's once@ one-shot) ─────────────


def _arm_watchdog(item_id: str, minutes: int) -> None:
    """Stage + approve a one-shot schedule that checks for a stall N minutes from now.

    Reuses the schedules skill's once@ format: the heartbeat dispatcher fires it once,
    runs `summon watchdog-check <item>`, forwards any stdout (the stall alert) to Discord,
    then the one-off retires itself. summon is operator-initiated, so the watchdog it arms
    is approved here rather than requiring a second manual approval. Best-effort: a
    scheduling failure never blocks the (already-launched) build.
    """
    if not SCHEDULES_SKILL.exists():
        print("schedules skill not found — watchdog not armed", file=sys.stderr)
        return
    fire_at = datetime.now(_LOCAL_TZ) + timedelta(minutes=minutes)
    schedule = "once@" + fire_at.strftime("%Y-%m-%dT%H:%M")
    try:
        proposed = subprocess.run(
            [
                "python3",
                str(SCHEDULES_SKILL),
                "propose",
                "--skill",
                "summon",
                "--schedule",
                schedule,
                "--description",
                f"dev-crew stall watchdog for {item_id}",
                "--args",
                json.dumps(["watchdog-check", item_id]),
                "--created-by",
                "summon",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proposed.returncode != 0:
            print(f"watchdog propose failed: {proposed.stderr.strip()}", file=sys.stderr)
            return
        job_id = json.loads(proposed.stdout)["id"]
        approved = subprocess.run(
            ["python3", str(SCHEDULES_SKILL), "approve", str(job_id)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if approved.returncode != 0:
            print(f"watchdog approve failed: {approved.stderr.strip()}", file=sys.stderr)
            return
        print(f"Watchdog armed: stall check at {schedule} local (in ~{minutes} min).")
    except Exception as exc:  # noqa: BLE001
        print(f"watchdog arming error: {exc}", file=sys.stderr)


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
    display_name = f"{persona['name']} — {item_id}"

    # ── isolated build worktree ────────────────────────────────────────────────
    # The builder must not disturb the operator's checkout, so it works in a dedicated
    # worktree. A worktree of the (trusted) repo inherits its trust, so the unattended
    # session is not blocked by the workspace-trust dialog.
    wt = worktree_path(repo_dir, item_id)
    subprocess.run(["git", "-C", repo_dir, "fetch", "origin"], capture_output=True, text=True)
    if wt.exists():
        # Remove a stale worktree from a previous summon of the same item.
        subprocess.run(
            ["git", "-C", repo_dir, "worktree", "remove", "--force", str(wt)],
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "-C", repo_dir, "worktree", "prune"], capture_output=True, text=True)
    add = subprocess.run(
        ["git", "-C", repo_dir, "worktree", "add", "--detach", str(wt), "origin/main"],
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        print(f"Failed to create build worktree at {wt}: {add.stderr.strip()}", file=sys.stderr)
        return 1

    url_file = Path(f"/tmp/claude-rc-{persona_id}.out")
    url_file.unlink(missing_ok=True)

    # Build the RC launch command via the Phase 1 auth wrapper. The kickoff is delivered
    # as a POSITIONAL prompt — the interactive RC session auto-submits it on startup.
    # No tmux send-keys: every argument is shell-quoted into one sh -c string.
    kickoff = build_kickoff(persona["name"], item_id)
    launch_args = [
        str(LAUNCH_SH),
        auth_profile,
        "--",
        "--remote-control",
        display_name,
        "--permission-mode",
        permission_mode,
        kickoff,
    ]
    rc_cmd = " ".join(shlex.quote(a) for a in launch_args)
    rc_cmd += f" > {shlex.quote(str(url_file))} 2>&1"

    print(f"Launching {persona['name']} on {item_id} in {wt} ...")
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", sname, "-c", str(wt), rc_cmd],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Failed to start tmux session: {exc}", file=sys.stderr)
        subprocess.run(
            ["git", "-C", repo_dir, "worktree", "remove", "--force", str(wt)],
            capture_output=True,
        )
        return 1

    _record_summon(item_id, persona_id, str(wt), datetime.now(UTC))
    _arm_watchdog(item_id, WATCHDOG_MINUTES)

    url = capture_rc_url(url_file, timeout=15)
    if url:
        print(f"RC session URL: {url}")
    else:
        print(
            "Warning: RC URL not captured within timeout — session is running; "
            "connect via claude.ai/code or the Claude mobile app",
            file=sys.stderr,
        )

    print(f"Kickoff delivered to session '{sname}' (auto-submitted; zero paste).")
    print(f"\nSession '{sname}' live. Link: {url or '(connect via claude.ai/code)'}")
    return 0


def cmd_ask(args: list[str]) -> int:
    """ask <id> <question...>  — builder posts a blocking question to the dev-loop Discord."""
    if len(args) < 2:
        print('Usage: ask <id> "<question>"', file=sys.stderr)
        return 1
    item_id = args[0]
    question = " ".join(args[1:]).strip()
    if not question:
        print("Error: empty question", file=sys.stderr)
        return 1

    conn = _db()
    try:
        row = conn.execute(
            "SELECT persona FROM dev_crew_runs WHERE item = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    persona = row[0] if row else "builder"

    message = f"🟡 **{persona}** is blocked on `{item_id}` and needs a decision:\n> {question}"
    posted = _post_discord(message)
    _set_question(item_id, datetime.now(UTC))

    if not posted:
        print("Warning: question recorded but Discord post failed — check webhook", file=sys.stderr)
        return 1
    print(f"Question posted to Discord for {item_id}.")
    return 0


def cmd_watchdog_check(args: list[str]) -> int:
    """watchdog-check <id>  — one-shot stall check fired by the heartbeat dispatcher.

    Prints a stall alert to stdout (the dispatcher forwards it to Discord) iff the build
    has neither opened a PR/pushed its branch nor posted a blocking question. Prints
    nothing otherwise. Armed as a once@ schedule at summon time, which then retires.
    """
    if not args:
        print("Usage: watchdog-check <id>", file=sys.stderr)
        return 1
    item_id = args[0]

    conn = _db()
    try:
        row = conn.execute(
            "SELECT persona, question_at FROM dev_crew_runs WHERE item = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        # No record — nothing to watch (e.g. the persona was dismissed). Silent.
        return 0

    persona_id, question_at = row
    persona = load_roster().get(persona_id, {})
    repo_dir = persona.get("repo_dir", str(PROJECT))
    progressed = _branch_progressed(repo_dir, item_id)

    if is_stalled({"question_at": question_at}, progressed):
        name = persona.get("name", persona_id)
        print(
            f"⚠️ Builder {name} appears stalled on {item_id} — no PR and no posted "
            f"question. Intervene: reconnect via claude.ai/code, or "
            f"`summon dismiss {persona_id}` and re-summon."
        )
    return 0


def cmd_dismiss(args: list[str]) -> int:
    """dismiss <persona>  — kill the tmux session and remove any build worktree(s)."""
    if not args:
        print("Usage: dismiss <persona>", file=sys.stderr)
        return 1
    persona_id = args[0]
    sname = session_name(persona_id)
    live = list_crew_sessions()
    killed = False
    if sname in live:
        kill_session(sname)
        killed = True

    # Remove worktrees recorded for this persona, then forget the runs.
    persona = load_roster().get(persona_id, {})
    repo_dir = persona.get("repo_dir", str(PROJECT))
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT worktree FROM dev_crew_runs WHERE persona = ?", (persona_id,)
        ).fetchall()
        for (wt,) in rows:
            if wt:
                subprocess.run(
                    ["git", "-C", repo_dir, "worktree", "remove", "--force", wt],
                    capture_output=True,
                )
        subprocess.run(["git", "-C", repo_dir, "worktree", "prune"], capture_output=True)
        conn.execute("DELETE FROM dev_crew_runs WHERE persona = ?", (persona_id,))
        conn.commit()
    finally:
        conn.close()

    if killed:
        print(f"Dismissed session '{sname}' and cleaned its worktree(s)")
    else:
        print(f"No live session for '{persona_id}' (worktrees, if any, cleaned)")
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
        print(
            "Usage: skill.py <summon|ask|watchdog-check|dismiss|reaper|status> [args...]",
            file=sys.stderr,
        )
        return 1

    cmd, *rest = sys.argv[1:]
    dispatch = {
        "summon": cmd_summon,
        "ask": cmd_ask,
        "watchdog-check": cmd_watchdog_check,
        "dismiss": cmd_dismiss,
        "reaper": cmd_reaper,
        "status": cmd_status,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(
            f"Unknown command '{cmd}'; use summon|ask|watchdog-check|dismiss|reaper|status",
            file=sys.stderr,
        )
        return 1
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
