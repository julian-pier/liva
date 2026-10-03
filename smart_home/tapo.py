import asyncio
from kasa import Discover
from smart_home.config import TAPO_USERNAME, TAPO_PASSWORD, TAPO_DEVICES


async def _get_device(name: str, retries: int = 3, delay: float = 0.6, timeout: float = 10.0):
    cfg = TAPO_DEVICES[name]
    last_error = None

    for attempt in range(1, retries + 1):
        device = None
        try:
            device = await Discover.discover_single(
                cfg["ip"],
                username=TAPO_USERNAME,
                password=TAPO_PASSWORD,
                timeout=timeout,
            )
            await device.update()
            return device
        except Exception as e:
            last_error = e
            if device is not None:
                try:
                    await device.disconnect()
                except Exception:
                    pass
            if attempt < retries:
                await asyncio.sleep(delay)

    raise last_error


async def turn_on(name: str):
    device = await _get_device(name)
    try:
        await device.turn_on()
        return {"ok": True, "device": name, "state": True}
    finally:
        await device.disconnect()


async def turn_off(name: str):
    device = await _get_device(name)
    try:
        await device.turn_off()
        return {"ok": True, "device": name, "state": False}
    finally:
        await device.disconnect()


async def get_state(name: str, *, retries: int = 3, delay: float = 0.6, timeout: float = 10.0):
    device = await _get_device(name, retries=retries, delay=delay, timeout=timeout)
    try:
        return {
            "ok": True,
            "device": name,
            "alias": device.alias,
            "state": device.is_on,
            "is_on": device.is_on,
            "ip": device.host,
        }
    finally:
        await device.disconnect()
