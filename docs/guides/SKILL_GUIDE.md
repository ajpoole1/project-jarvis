# SKILL_GUIDE.md — How to build a Jarvis skill

status: canonical. Convention lens — see `QA_LENSES.md` §3 for how this file is consumed.
scope: **project-jarvis specific.** Jarvis paths, conventions, and examples throughout.

---

## Skill anatomy

Every skill lives in `skills/<name>/` and is entered via `skill.py`:

```
skills/
└── my-skill/
    ├── skill.py          # entry point — OpenClaw shells out to this
    ├── requirements.txt  # dependencies (or stdlib-only note)
    └── README.md         # what it does, env vars, how to test
```

`skill.py` receives its subcommand as `sys.argv[1]` and any arguments as subsequent argv elements. All command routing goes through a single `if/elif` chain at the bottom of the file in a `main()` function. No argparse required for simple skills — direct argv inspection is fine.

```python
def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        cmd_add(sys.argv[2:])
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

Conforming example: `skills/followups/skill.py` — clear command table, stdlib only, argv validated before use.

## Virtualenv decision rule

No judgment call, no exceptions:

| Has any third-party import? | Action |
|---|---|
| No (stdlib only) | No venv. Document in `requirements.txt` with a comment: `# No virtualenv needed — stdlib only`. |
| Yes (any single third-party package) | Create `.venv/` in the skill directory. Pin every dependency in `requirements.txt`. |

The dispatcher selects `skills/<name>/.venv/bin/python` if present, falling back to system `python3`. A stdlib-only skill must work with system `python3` without activation.

Conforming stdlib example: `skills/followups/` — `requirements.txt` documents the stdlib-only decision explicitly.
Conforming venv example: `skills/gmail-cleanup/` — Google API + anthropic packages pinned in `requirements.txt`, venv present.

## Argv validation

Validate argv before acting. A missing required argument is a hard error, not a silent no-op:

```python
if len(sys.argv) < 3:
    print("usage: skill.py add <text>", file=sys.stderr)
    sys.exit(1)
```

Never trust argv values from external sources (Discord input, cron args) without stripping or sanitizing before use in SQL or subprocess calls.

## Logging

Log to `/logs/<skill-name>.log`. Use the stdlib `logging` module. Log what actions were taken, never what data was processed.

```python
import logging
logging.basicConfig(
    filename=f"/logs/my-skill.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logging.info("added item id=%s", item_id)        # correct — action + opaque id
logging.info("added item text=%r", item_text)    # wrong — PII in log
```

**No PII in logs.** Redact email subjects, names, message bodies, and any user-supplied text content. Log IDs, counts, and status codes only.

## SQLite

Use `/data/jarvis.db` via the `JARVIS_DATA_DIR` environment variable:

```python
_DB_PATH = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"
```

Never create a skill-private database. All state goes into the shared schema.

**Exception (explicit architectural decision only):** large or sensitive multi-table domain systems (e.g. `finance/`) may use a dedicated DB file (`/data/finance.db`). This requires a documented decision in the skill's README — it is not a default option for any new skill.

Use parameterized queries only — never string-format SQL:

```python
cur.execute("INSERT INTO items (text) VALUES (?)", (text,))   # correct
cur.execute(f"INSERT INTO items (text) VALUES ('{text}')")     # never — SQL injection
```

Conforming example: `skills/followups/skill.py` — all queries parameterized, DB path via env.

## Secrets

All secrets via `~/.jarvis.env`. Read with a manual env-file parse (no `python-dotenv` for stdlib-only skills):

```python
_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())
```

For skills with a venv, `python-dotenv` may be used instead — but only if it is already a dependency for another reason. Do not add it solely for env loading.

Never hardcode a secret. Never read from a path under `/mnt/c/` — DrvFs does not enforce file permissions.

## Stage-then-approve for consequential actions

Any action that is hard to reverse (send email, delete record, place order, post to external API) must be staged and presented to the operator before execution. The pattern:

1. Compute the proposed action.
2. Print or post a human-readable summary to Discord.
3. Wait for explicit operator confirmation (a subsequent command invocation, not a timeout).
4. Execute only after confirmation.

Never take a consequential action autonomously. Conforming example: `skills/gmail-cleanup/` — stage/execute split with explicit `execute` subcommand gated on operator approval.

## Discord output

Post to Discord via `scripts/discord_post.py`. Never implement a per-skill webhook call:

```python
subprocess.run(
    [sys.executable, str(_DISCORD_SCRIPT), message],
    check=True,
)
```

The `discord_post.py` script handles chunking, rate-limit retry, and channel routing. Skills produce output; they do not manage the transport.

---

## Review Checklist

- [ ] Skill lives in `skills/<name>/` with `skill.py`, `requirements.txt`, and `README.md`.
- [ ] All command routing through a single `main()` function with `if/elif` on `sys.argv[1]`.
- [ ] No venv for stdlib-only skills; `requirements.txt` documents the decision with a comment.
- [ ] Any third-party import → `.venv/` present and all deps pinned in `requirements.txt`.
- [ ] Missing or invalid argv causes a `print(..., file=sys.stderr)` + `sys.exit(1)`, not a silent no-op.
- [ ] Logging via stdlib `logging` to `/logs/<skill-name>.log`; no user-content or PII in log messages.
- [ ] DB path resolved via `JARVIS_DATA_DIR` env var; no hardcoded `/data/` paths.
- [ ] All SQL queries parameterized with `?` placeholders; no f-string or format-string SQL.
- [ ] No skill-private DB file without an explicit architectural decision documented in the README.
- [ ] All secrets read from `~/.jarvis.env`; no hardcoded credentials; no reads from `/mnt/c/` paths.
- [ ] Consequential actions (send, delete, post, purchase) are staged for operator confirmation before execution.
- [ ] Discord output routed through `scripts/discord_post.py`; no per-skill webhook implementation.
