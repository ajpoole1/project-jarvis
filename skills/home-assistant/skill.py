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
ENTITY_FAN_DISPLAY = "switch.ellie_s_register_fan_display"


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
        try:
            temp = round((float(_state(ENTITY_FAN_TEMP)) - 32) * 5 / 9, 1)
        except (ValueError, TypeError):
            temp = _state(ENTITY_FAN_TEMP)
        speed = _state(ENTITY_FAN_SPEED)
        mode = _state(ENTITY_FAN_MODE)
        child_lock = _state(ENTITY_FAN_CHILDLOCK)
        display = _state(ENTITY_FAN_DISPLAY)
        try:
            target = round((float(_state(ENTITY_FAN_TARGET_TEMP)) - 32) * 5 / 9, 1)
        except (ValueError, TypeError):
            target = _state(ENTITY_FAN_TARGET_TEMP)
        return (
            f"**Fan status**\n"
            f"Power: {power}\n"
            f"Mode: {mode}\n"
            f"Indoor temp: {temp}°C\n"
            f"Target temp: {target}°C\n"
            f"Speed: {speed}/10\n"
            f"Child lock: {child_lock}\n"
            f"Display: {display}"
        )

    sub = args[0].lower()

    if sub in ("on", "off"):
        service = "turn_on" if sub == "on" else "turn_off"
        _service("switch", service, entity_id=ENTITY_FAN_POWER)
        return f"Fan turned {sub}."

    if sub == "temp":
        if len(args) < 2:
            try:
                temp = round((float(_state(ENTITY_FAN_TEMP)) - 32) * 5 / 9, 1)
            except (ValueError, TypeError):
                temp = _state(ENTITY_FAN_TEMP)
            return f"Current indoor temp: {temp}°C"
        try:
            temp_c = float(args[1])
        except ValueError:
            return "Usage: fan temp <degrees C>"
        if not 0 <= temp_c <= 50:
            return "Temperature must be between 0–50°C."
        temp_f = round(temp_c * 9 / 5 + 32)
        _service("number", "set_value", entity_id=ENTITY_FAN_TARGET_TEMP, value=temp_f)
        return f"Fan target temp set to {temp_c}°C."

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

    if sub == "display":
        state = args[1].lower() if len(args) > 1 else None
        if state is None:
            return f"Display is {_state(ENTITY_FAN_DISPLAY)}."
        if state not in ("on", "off"):
            return "Usage: fan display on|off"
        service = "turn_on" if state == "on" else "turn_off"
        _service("switch", service, entity_id=ENTITY_FAN_DISPLAY)
        return f"Fan display turned {state}."

    if sub == "status":
        return cmd_fan([])

    return (
        "Unknown fan command. Available:\n"
        "  `fan` — status\n"
        "  `fan on` / `fan off`\n"
        "  `fan temp <F>` — set target temperature\n"
        "  `fan mode <Fan|Cool|Heat|Sleep>` — set mode\n"
        "  `fan speed <1-10>` — set speed\n"
        "  `fan lock on|off` — child lock\n"
        "  `fan display on|off` — display brightness"
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
