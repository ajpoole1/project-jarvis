# gateway-watchdog

Samples `openclaw-gateway` RSS on each scheduler tick and posts a Discord alert when it exceeds a threshold. Early warning before OOM — the memory analog of the `last_tick` heartbeat guard.

## Commands

- `check` — sample RSS, alert if above threshold (default)

## Environment

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_RSS_WARN_MB` | `1500` | Alert threshold in MB |

## Schedule

Add via `schedules propose` — runs on the existing 10m heartbeat tick:

```
skill: gateway-watchdog
args: ["check"]
schedule: 10m
description: Gateway RSS watchdog — alert if openclaw-gateway exceeds 1.5GB
```

## How to test

```bash
python3 skills/gateway-watchdog/skill.py check
```

If gateway is running below threshold: no output (silent success).
If above threshold: posts Discord alert and prints warning.

## Notes

- Does not fix the leak — it's a monitoring layer independent of root cause
- stdlib only, no venv needed
- Threshold tunable via `GATEWAY_RSS_WARN_MB` in `~/.jarvis.env`
