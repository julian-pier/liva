import asyncio
from smart_home.tapo import turn_on as tapo_turn_on
from smart_home.tapo import turn_off as tapo_turn_off
from smart_home.tapo import get_state as tapo_get_state


def run(coro):
    return asyncio.run(coro)


def turn_on(device_name: str):
    return run(tapo_turn_on(device_name))


def turn_off(device_name: str):
    return run(tapo_turn_off(device_name))


def get_state(device_name: str, *, retries: int = 3, delay: float = 0.6, timeout: float = 10.0):
    return run(tapo_get_state(device_name, retries=retries, delay=delay, timeout=timeout))
