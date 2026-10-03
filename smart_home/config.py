import json
import os


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _device_map(name: str) -> dict[str, dict[str, str]]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain valid JSON") from exc
    if not isinstance(parsed, dict) or not all(isinstance(value, dict) for value in parsed.values()):
        raise RuntimeError(f"{name} must be a JSON object of device objects")
    return parsed


TAPO_USERNAME = os.getenv("LIVA_TAPO_USERNAME", "").strip()
TAPO_PASSWORD = os.getenv("LIVA_TAPO_PASSWORD", "")
TAPO_DEVICES = _device_map("LIVA_TAPO_DEVICES_JSON")
GOVEE_DEVICE_DEFINITIONS = _device_map("LIVA_GOVEE_DEVICES_JSON")
LIVA_GOVEE_ENABLED = _env_flag("LIVA_GOVEE_ENABLED", default=False)
GOVEE_DEVICES = dict(GOVEE_DEVICE_DEFINITIONS) if LIVA_GOVEE_ENABLED else {}


def is_govee_enabled() -> bool:
    return bool(LIVA_GOVEE_ENABLED)
