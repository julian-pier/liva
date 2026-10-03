from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smart_home.config import is_govee_enabled
from smart_home.service import get_state as tapo_get_state
from smart_home.service import turn_off as tapo_off
from smart_home.service import turn_on as tapo_on

if is_govee_enabled():
    from smart_home.govee import (
        get_state as govee_get_state,
        set_brightness as govee_set_brightness,
        set_color_rgb as govee_set_color_rgb,
        turn_off as govee_off,
        turn_on as govee_on,
    )
else:
    govee_get_state = None
    govee_set_brightness = None
    govee_set_color_rgb = None
    govee_off = None
    govee_on = None

LOG = logging.getLogger(__name__)
LOG_PREFIX = "[core-intervention-light]"
ROOT_DIR = Path(__file__).resolve().parents[1]
REMOTE_STATE_PATH = ROOT_DIR / "smart_home" / "data" / "remote_devices_state.json"


def _safe_govee_state(name: str, *, timeout: float = 1.5, retries: int = 2) -> dict[str, Any] | None:
    if not is_govee_enabled():
        return None
    last_exc: Exception | None = None
    for _ in range(max(1, int(retries))):
        try:
            state = govee_get_state(name, timeout=timeout)
            return state if isinstance(state, dict) else None
        except Exception as exc:
            last_exc = exc
            time.sleep(0.15)
    LOG.warning("%s govee state failed (%s): %s", LOG_PREFIX, name, last_exc)
    return None


def _safe_tapo_state(name: str) -> dict[str, Any] | None:
    try:
        state = tapo_get_state(name)
        return state if isinstance(state, dict) else None
    except Exception as exc:
        LOG.warning("%s tapo state failed (%s): %s", LOG_PREFIX, name, exc)
        return None


def _restore_govee(name: str, state: dict[str, Any] | None) -> None:
    if not is_govee_enabled():
        return
    if not state:
        return
    try:
        if state.get("is_on") is False:
            govee_off(name)
            return
        if state.get("is_on") is True:
            govee_on(name)
        brightness = state.get("brightness")
        if isinstance(brightness, int):
            govee_set_brightness(name, brightness)
        color = state.get("color")
        if isinstance(color, dict):
            r = color.get("r")
            g = color.get("g")
            b = color.get("b")
            if all(isinstance(v, int) for v in (r, g, b)):
                govee_set_color_rgb(name, int(r), int(g), int(b))
    except Exception as exc:
        LOG.warning("%s govee restore failed (%s): %s", LOG_PREFIX, name, exc)


def _load_remote_device_state(name: str, *, max_age_s: float = 7200.0) -> dict[str, Any] | None:
    if not REMOTE_STATE_PATH.exists():
        return None
    try:
        payload = json.loads(REMOTE_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    row = payload.get(name) if isinstance(payload, dict) else None
    if not isinstance(row, dict):
        return None
    updated_at = row.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        try:
            if updated_at.endswith("Z"):
                updated_at = updated_at.replace("Z", "+00:00")
            ts = datetime.fromisoformat(updated_at).astimezone(timezone.utc)
            if (datetime.now(timezone.utc) - ts).total_seconds() > max(1.0, float(max_age_s)):
                return None
        except Exception:
            return None
    is_on = row.get("is_on")
    if isinstance(is_on, bool):
        return {"ok": True, "is_on": is_on, "brightness": None, "color": None, "source": "remote_cache"}
    return None


def _force_govee_off(name: str, *, retries: int = 3, delay_s: float = 0.25) -> None:
    if not is_govee_enabled():
        return
    last_exc: Exception | None = None
    for _ in range(max(1, int(retries))):
        try:
            govee_off(name)
            time.sleep(max(0.05, float(delay_s)))
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(max(0.05, float(delay_s)))
    if last_exc:
        LOG.warning("%s govee forced off failed (%s): %s", LOG_PREFIX, name, last_exc)


def _govee_dim_blue(
    name: str,
    *,
    hold_s: float = 3.0,
    brightness_high: int = 90,
    brightness_low: int = 10,
) -> None:
    if not is_govee_enabled():
        return
    govee_on(name)
    govee_set_color_rgb(name, 30, 60, 200)
    govee_set_brightness(name, brightness_high)
    time.sleep(max(0.1, float(hold_s)))
    govee_set_brightness(name, brightness_low)


def _apply_govee_state(name: str, state: dict[str, Any], *, retries: int = 2, delay_s: float = 0.2) -> None:
    if not is_govee_enabled():
        return
    last_exc: Exception | None = None
    for _ in range(max(1, int(retries))):
        try:
            if state.get("is_on") is False:
                govee_off(name)
                return
            if state.get("is_on") is True:
                govee_on(name)
            brightness = state.get("brightness")
            if isinstance(brightness, int):
                govee_set_brightness(name, brightness)
            color = state.get("color")
            if isinstance(color, dict):
                r = color.get("r")
                g = color.get("g")
                b = color.get("b")
                if all(isinstance(v, int) for v in (r, g, b)):
                    govee_set_color_rgb(name, int(r), int(g), int(b))
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(max(0.05, delay_s))
    LOG.warning("%s govee apply fallback failed (%s): %s", LOG_PREFIX, name, last_exc)


def _restore_tapo(name: str, state: dict[str, Any] | None) -> None:
    if not state:
        return
    try:
        if bool(state.get("state")):
            tapo_on(name)
        else:
            tapo_off(name)
    except Exception as exc:
        LOG.warning("%s tapo restore failed (%s): %s", LOG_PREFIX, name, exc)


def _govee_pulse(
    name: str,
    *,
    pulses: int,
    color: dict[str, int],
    brightness: int,
    on_s: float,
    off_s: float,
    restore_state: dict[str, Any] | None = None,
    restore_retries: int = 2,
    restore_delay_s: float = 0.2,
) -> None:
    if not is_govee_enabled():
        return
    govee_on(name)
    for idx in range(max(1, pulses)):
        govee_set_color_rgb(name, color["r"], color["g"], color["b"])
        govee_set_brightness(name, brightness)
        time.sleep(max(0.05, on_s))
        if restore_state:
            _apply_govee_state(name, restore_state, retries=restore_retries, delay_s=restore_delay_s)
        else:
            govee_off(name)
        time.sleep(max(0.05, off_s))


def _tapo_pulse(
    name: str,
    *,
    pulses: int,
    on_s: float,
    off_s: float,
) -> None:
    for idx in range(max(1, pulses)):
        tapo_on(name)
        time.sleep(max(0.05, on_s))
        tapo_off(name)
        if idx < pulses - 1:
            time.sleep(max(0.05, off_s))


def pulse_light(
    *,
    pulses: int = 2,
    color: dict[str, int] | None = None,
    brightness: int = 85,
    on_s: float = 0.35,
    off_s: float = 0.25,
    govee_device: str = "desk_strip",
    tapo_device: str = "schreibtischlampe",
    allow_tapo_fallback: bool = False,
    govee_state_timeout: float = 1.5,
    govee_state_retries: int = 2,
    force_govee_pulse_without_state: bool = False,
    fallback_restore_state: dict[str, Any] | None = None,
    restore_retries: int = 2,
    restore_delay_s: float = 0.2,
    skip_state_fetch_when_fallback: bool = False,
    prefer_off_when_state_unknown: bool = False,
    final_off_retries: int = 3,
    final_off_delay_s: float = 0.25,
    use_cached_state_on_fail: bool = True,
    cached_state_max_age_s: float = 120.0,
    unknown_state_restore: str = "dim_blue",
) -> dict[str, Any]:
    color = color or {"r": 255, "g": 120, "b": 40}
    brightness = max(1, min(100, int(brightness)))
    pulses = max(1, int(pulses))
    on_s = float(on_s)
    off_s = float(off_s)

    result: dict[str, Any] = {
        "ok": False,
        "used": None,
        "govee": {"ok": False, "disabled": not is_govee_enabled()},
        "tapo": {"ok": False},
    }

    if not is_govee_enabled():
        if not allow_tapo_fallback:
            return result
        tapo_state = _safe_tapo_state(tapo_device)
        if tapo_state and tapo_state.get("ok"):
            try:
                _tapo_pulse(tapo_device, pulses=pulses, on_s=on_s, off_s=off_s)
                result["used"] = "tapo"
                result["tapo"] = {"ok": True}
                result["ok"] = True
            except Exception as exc:
                LOG.warning("%s tapo pulse failed (%s): %s", LOG_PREFIX, tapo_device, exc)
            finally:
                _restore_tapo(tapo_device, tapo_state)
        return result

    govee_state = None
    if not (fallback_restore_state and skip_state_fetch_when_fallback):
        govee_state = _safe_govee_state(
            govee_device,
            timeout=govee_state_timeout,
            retries=govee_state_retries,
        )
    if not govee_state and use_cached_state_on_fail:
        cached = _load_remote_device_state(govee_device, max_age_s=cached_state_max_age_s)
        if cached:
            govee_state = cached

    if govee_state and (govee_state.get("ok") is None or govee_state.get("ok") is True):
        was_off = govee_state.get("is_on") is False
        try:
            _govee_pulse(
                govee_device,
                pulses=pulses,
                color=color,
                brightness=brightness,
                on_s=on_s,
                off_s=off_s,
                restore_state=govee_state,
                restore_retries=restore_retries,
                restore_delay_s=restore_delay_s,
            )
            result["used"] = "govee"
            result["govee"] = {"ok": True}
            result["ok"] = True
        except Exception as exc:
            LOG.warning("%s govee pulse failed (%s): %s", LOG_PREFIX, govee_device, exc)
        finally:
            _restore_govee(govee_device, govee_state)
            if was_off:
                _force_govee_off(govee_device, retries=final_off_retries, delay_s=final_off_delay_s)
    elif force_govee_pulse_without_state or fallback_restore_state:
        restore_mode = str(unknown_state_restore or "dim_blue").strip().lower()
        if restore_mode in {"dim_blue", "blue_dim"}:
            try:
                _govee_dim_blue(govee_device, hold_s=3.0, brightness_high=70, brightness_low=10)
                result["used"] = "govee"
                result["govee"] = {"ok": True, "restored": True, "restored_via_dim_blue": True}
                result["ok"] = True
            except Exception as exc:
                LOG.warning("%s govee dim-blue failed (%s): %s", LOG_PREFIX, govee_device, exc)
        if result["ok"]:
            return result

        try:
            _govee_pulse(
                govee_device,
                pulses=pulses,
                color=color,
                brightness=brightness,
                on_s=on_s,
                off_s=off_s,
                restore_state=fallback_restore_state,
                restore_retries=restore_retries,
                restore_delay_s=restore_delay_s,
            )
            result["used"] = "govee"
            result["govee"] = {"ok": True, "restored": False}
            result["ok"] = True
        except Exception as exc:
            LOG.warning("%s govee pulse (no-state) failed (%s): %s", LOG_PREFIX, govee_device, exc)
        else:
            if restore_mode in {"on", "keep_on"}:
                if fallback_restore_state:
                    _apply_govee_state(govee_device, fallback_restore_state)
                    result["govee"]["restored"] = True
                    result["govee"]["restored_via_fallback"] = True
            elif restore_mode in {"dim_blue", "blue_dim"}:
                try:
                    _govee_dim_blue(govee_device, hold_s=3.0, brightness_high=70, brightness_low=10)
                    result["govee"]["restored"] = True
                    result["govee"]["restored_via_dim_blue"] = True
                except Exception as exc:
                    LOG.warning("%s govee dim-blue failed (%s): %s", LOG_PREFIX, govee_device, exc)
            elif restore_mode in {"dim", "warn", "minimal"}:
                dim_state = dict(fallback_restore_state or {})
                dim_state.setdefault("is_on", True)
                dim_state.setdefault("color", {"r": 255, "g": 180, "b": 120})
                dim_state["brightness"] = max(5, min(25, int(dim_state.get("brightness") or 20)))
                _apply_govee_state(govee_device, dim_state)
                result["govee"]["restored"] = True
                result["govee"]["restored_via_dim"] = True
            elif restore_mode in {"none", "leave"}:
                result["govee"]["restored"] = False
                result["govee"]["restored_via_none"] = True
            else:
                _force_govee_off(govee_device, retries=final_off_retries, delay_s=final_off_delay_s)
                result["govee"]["restored"] = True
                result["govee"]["restored_via_off"] = True

    if result["ok"]:
        return result

    if not allow_tapo_fallback:
        return result

    tapo_state = _safe_tapo_state(tapo_device)
    if tapo_state and tapo_state.get("ok"):
        try:
            _tapo_pulse(tapo_device, pulses=pulses, on_s=on_s, off_s=off_s)
            result["used"] = "tapo"
            result["tapo"] = {"ok": True}
            result["ok"] = True
        except Exception as exc:
            LOG.warning("%s tapo pulse failed (%s): %s", LOG_PREFIX, tapo_device, exc)
        finally:
            _restore_tapo(tapo_device, tapo_state)

    return result
