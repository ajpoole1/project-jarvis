import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.home() / ".jarvis.env")

HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]

ENTITY_FAN_POWER = "switch.ellie_s_register_fan_power"
ENTITY_FAN_TEMP = "sensor.ellie_s_register_fan_indoor_temp"
ENTITY_FAN_SPEED = "select.ellie_s_register_fan_fan_speed"
ENTITY_FAN_MODE = "select.ellie_s_register_fan_mode"
ENTITY_FAN_CHILDLOCK = "switch.ellie_s_register_fan_child_lock"
ENTITY_FAN_TARGET_TEMP = "number.ellie_s_register_fan_target_temp"


def _ha(method, path, data=None):
    url = f"{HA_URL}/api/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.reason, "code": e.code}


def _state(entity_id):
    result = _ha("GET", f"states/{entity_id}")
    return result.get("state", "unknown")


def _service(domain, service, **kwargs):
    _ha("POST", f"services/{domain}/{service}", kwargs)


def cmd_fan(args):
    if not args:
        power = _state(ENTITY_FAN_POWER)
        temp = _state(ENTITY_FAN_TEMP)
        speed = _state(ENTITY_FAN_SPEED)
        mode = _state(ENTITY_FAN_MODE)
        target = _state(ENTITY_FAN_TARGET_TEMP)
        return (
            f"**Fan status**\n"
            f"Power: {power}\n"
            f"Mode: {mode}\n"
            f"Indoor temp: {temp}°F\n"
            f"Target temp: {target}°F\n"
            f"Speed: {speed}/10"
        )

    sub = args[0].lower()

    if sub in ("on", "off"):
        service = "turn_on" if sub == "on" else "turn_off"
        _service("switch", service, entity_id=ENTITY_FAN_POWER)
        return f"Fan turned {sub}."

    if sub == "temp":
        if len(args) < 2:
            current = _state(ENTITY_FAN_TEMP)
            return f"Current indoor temp: {current}°F"
        try:
            temp = int(args[1])
        except ValueError:
            return "Usage: fan temp <degrees F>"
        if not 32 <= temp <= 122:
            return "Temperature must be between 32–122°F."
        _service("number", "set_value", entity_id=ENTITY_FAN_TARGET_TEMP, value=temp)
        return f"Fan target temp set to {temp}°F."

    if sub == "mode":
        if len(args) < 2:
            return f"Current mode: {_state(ENTITY_FAN_MODE)}"
        mode = args[1].capitalize()
        if mode not in ("Fan", "Cool", "Heat", "Sleep"):
            return "Mode must be: Fan, Cool, Heat, or Sleep."
        _service("select", "select_option", entity_id=ENTITY_FAN_MODE, option=mode)
        return f"Fan mode set to {mode}."

    if sub == "speed":
        if len(args) < 2:
            return f"Current speed: {_state(ENTITY_FAN_SPEED)}/10"
        speed = args[1]
        _service("select", "select_option", entity_id=ENTITY_FAN_SPEED, option=speed)
        return f"Fan speed set to {speed}."

    if sub == "lock":
        state = args[1].lower() if len(args) > 1 else "on"
        if state not in ("on", "off"):
            return "Usage: fan lock on|off"
        service = "turn_on" if state == "on" else "turn_off"
        _service("switch", service, entity_id=ENTITY_FAN_CHILDLOCK)
        return f"Fan child lock turned {state}."

    if sub == "status":
        return cmd_fan([])

    return (
        "Unknown fan command. Available:\n"
        "  `fan` — status\n"
        "  `fan on` / `fan off`\n"
        "  `fan temp <F>` — set target temperature\n"
        "  `fan mode <Fan|Cool|Heat|Sleep>` — set mode\n"
        "  `fan speed <1-10>` — set speed\n"
        "  `fan lock on|off` — child lock"
    )


def main():
    args = sys.argv[1:]
    if not args:
        print(cmd_fan([]))
        return

    cmd = args[0].lower()

    if cmd == "fan":
        print(cmd_fan(args[1:]))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
