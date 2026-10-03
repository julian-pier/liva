import asyncio
from smart_home.tapo import get_state, turn_off, turn_on


async def main():
    print(await get_state("monitor_links"))

    print("Schalte aus ...")
    print(await turn_off("monitor_links"))

    print("Schalte an ...")
    print(await turn_on("monitor_links"))

    print(await get_state("monitor_links"))


asyncio.run(main())