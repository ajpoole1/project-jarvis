# home-assistant skill

Controls smart home devices via the Home Assistant REST API.

## Env vars (in `~/.jarvis.env`)

| Var | Description |
|---|---|
| `HA_URL` | HA base URL, e.g. `http://localhost:8123` |
| `HA_TOKEN` | Long-lived access token from HA profile page |

## Commands

| Command | Description |
|---|---|
| `fan` | Show fan status (power, mode, temp, speed) |
| `fan on` / `fan off` | Turn fan on or off |
| `fan temp <F>` | Set target temperature (32–122°F) |
| `fan mode <Fan\|Cool\|Heat\|Sleep>` | Set operating mode |
| `fan speed <1-10>` | Set fan speed |
| `fan lock on\|off` | Toggle child lock |

## Device notes

Fan is a register booster fan (XFBD410, Tuya category `xfj`) at `192.168.2.39`.
Controlled via LocalTuya (xZetsubou/hass-localtuya 2025.11.0) using protocol 3.5 over the local LAN — no cloud dependency.

HA runs as a systemd service in WSL2 (`/srv/homeassistant` venv) with mirrored networking enabled (`~/.wslconfig`), which gives it direct LAN access for LocalTuya UDP discovery and TCP control.

DP map for reference: DP1=switch, DP2=mode, DP9=temp_indoor (ro), DP12=fan_speed_enum, DP14=child_lock, DP45=temp_set, DP102=brightscreen_set.

## HA entities

| Entity | Type | Notes |
|---|---|---|
| `switch.fan_power` | switch | on/off via command_line |
| `sensor.fan_indoor_temp` | sensor | °F, polled every 120s |
| `sensor.fan_speed` | sensor | 1–10, read-only |
| `sensor.fan_mode` | sensor | FAN/COOL/HEAT/SLEEP |

## Test

```bash
source skills/home-assistant/.venv/bin/activate
python skills/home-assistant/skill.py fan
python skills/home-assistant/skill.py fan on
python skills/home-assistant/skill.py fan temp 71
```
