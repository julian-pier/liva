import json
import socket
from smart_home.config import GOVEE_DEVICES, is_govee_enabled

GOVEE_PORT = 4003


def _send_command(name: str, payload: dict, expect_response: bool = False, timeout: float = 0.7):
    if not is_govee_enabled():
        raise RuntimeError("Govee is disabled by LIVA_GOVEE_ENABLED")
    cfg = GOVEE_DEVICES[name]
    message = json.dumps(payload).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if expect_response:
            sock.bind(("", 0))
            sock.settimeout(max(0.1, float(timeout)))
        sock.sendto(message, (cfg["ip"], GOVEE_PORT))
        if expect_response:
            data, _addr = sock.recvfrom(4096)
            text = data.decode("utf-8", errors="replace").strip()
            parsed = json.loads(text) if text else {}
            return {"ok": True, "device": name, "payload": payload, "response": parsed}
    finally:
        sock.close()

    return {"ok": True, "device": name, "payload": payload}


def turn_on(name: str):
    return _send_command(name, {
        "msg": {
            "cmd": "turn",
            "data": {
                "value": 1
            }
        }
    })


def turn_off(name: str):
    return _send_command(name, {
        "msg": {
            "cmd": "turn",
            "data": {
                "value": 0
            }
        }
    })


def set_brightness(name: str, brightness: int):
    brightness = max(1, min(100, int(brightness)))
    return _send_command(name, {
        "msg": {
            "cmd": "brightness",
            "data": {
                "value": brightness
            }
        }
    })


def set_color_rgb(name: str, r: int, g: int, b: int):
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))

    return _send_command(name, {
        "msg": {
            "cmd": "colorwc",
            "data": {
                "color": {
                    "r": r,
                    "g": g,
                    "b": b
                },
                "colorTemInKelvin": 0
            }
        }
    })


def get_state(name: str, timeout: float = 0.7):
    result = _send_command(
        name,
        {
            "msg": {
                "cmd": "devStatus",
                "data": {},
            }
        },
        expect_response=True,
        timeout=timeout,
    )
    response = result.get("response") or {}
    msg = response.get("msg") if isinstance(response, dict) else {}
    data = msg.get("data") if isinstance(msg, dict) else {}
    if not isinstance(data, dict):
        data = {}

    on_raw = data.get("onOff")
    if on_raw is None:
        on_raw = data.get("powerState")
    if on_raw is None:
        on_raw = data.get("state")
    is_on = None
    if isinstance(on_raw, bool):
        is_on = on_raw
    elif isinstance(on_raw, (int, float)):
        is_on = int(on_raw) == 1
    elif isinstance(on_raw, str):
        val = on_raw.strip().lower()
        if val in {"on", "1", "true"}:
            is_on = True
        elif val in {"off", "0", "false"}:
            is_on = False

    brightness = data.get("brightness")
    try:
        brightness = int(brightness) if brightness is not None else None
    except Exception:
        brightness = None
    if isinstance(brightness, int):
        brightness = max(1, min(100, brightness))

    color = data.get("color")
    if not isinstance(color, dict):
        color = {}
    try:
        r = max(0, min(255, int(color.get("r"))))
        g = max(0, min(255, int(color.get("g"))))
        b = max(0, min(255, int(color.get("b"))))
        color_rgb = {"r": r, "g": g, "b": b}
    except Exception:
        color_rgb = None

    return {
        "ok": True,
        "device": name,
        "is_on": is_on,
        "brightness": brightness,
        "color": color_rgb,
        "raw": response,
    }
