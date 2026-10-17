"""
Diagnostic tool: connect to the real Click v2, activate it, and print every
decoded button press/release using the confirmed protocol in constants.py /
ble_client.py / protocol.py.

Usage:
    zwift-click-spike
"""
from __future__ import annotations

import asyncio
import logging

from . import constants
from .ble_client import ZwiftClickClient, find_device

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("spike")


def _describe(bitmask: int) -> str:
    up = "pressed" if (bitmask & constants.BUTTON_BIT_SHIFT_UP) == 0 else "released"
    down = "pressed" if (bitmask & constants.BUTTON_BIT_SHIFT_DOWN) == 0 else "released"
    return f"shift_up={up} shift_down={down}"


async def run() -> None:
    device = await find_device()
    logger.info("Found %s (%s)", device.name, device.address)

    def on_button(bitmask: int) -> None:
        logger.info("bitmask=0x%x  %s", bitmask, _describe(bitmask))

    client = ZwiftClickClient(device, on_button)
    await client.connect()
    logger.info("Connected -- press the Click's paddles now. Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await client.disconnect()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
