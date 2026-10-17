"""
Protocol-agnostic raw sniffer, round 2.

Findings from round 1 + public RE of Click v2 specifically (zwiftboard,
qdomyos-zwift, bikecontrol/swiftcontrol):
  - A Click that's already been unlocked by the real Zwift app at some point
    persists that unlock on-device. It does NOT need a fresh ECDH handshake
    -- that's only for a never-unlocked device (which replies with a 0xFF
    crypto challenge instead of an 0x52 'R' echo, per swiftcontrol #68).
  - Our Click echoed "RideOn\x02\x03..." (starts with 0x52) => already unlocked.
  - The correct host "request" for an unlocked device is just the short
    3-write activation trio below -- no 64-byte public key needed.
  - Button frames use opcode 0x23 (not 0x07), field 1 = a BITMASK
    (0 = pressed per bit), not independent tag/value pairs per button.
  - Device needs a bare "RideOn" keepalive write every ~3s or it goes idle
    and the LEFT pod disconnects after ~60s (per qdomyos PR #4743 / issue #68).

Usage:
    zwift-click-sniff
"""
from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient

from . import constants
from .ble_client import find_device
from .protocol import _read_varint

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sniff")

LISTEN_SECONDS = 40.0
KEEPALIVE_INTERVAL = 3.0

ACTIVATION_WRITES = [
    constants.RIDE_ON + bytes([0x02, 0x03]),
    bytes([0x00, 0x08, 0x00]),
    bytes([0x00, 0x08, 0x10]),
]
KEEPALIVE_WRITE = constants.RIDE_ON
BUTTON_OPCODE = 0x23


def _try_decode_button_frame(payload: bytes) -> None:
    if not payload or payload[0] != BUTTON_OPCODE:
        return
    offset = 1
    try:
        while offset < len(payload):
            tag, offset = _read_varint(payload, offset)
            field_number, wire_type = tag >> 3, tag & 0x7
            if wire_type != 0:
                logger.info("  (button frame has non-varint field %d, wire_type %d -- stopping decode)", field_number, wire_type)
                return
            value, offset = _read_varint(payload, offset)
            logger.info("  decoded field=%d value=%d (0x%x) bits=%s", field_number, value, value, format(value, "016b"))
    except IndexError:
        logger.info("  (ran out of bytes decoding button frame)")


def _log_frame(source: str, data: bytearray) -> None:
    raw = bytes(data)
    logger.info("[%s] len=%d hex=%s", source, len(raw), raw.hex())
    _try_decode_button_frame(raw)


async def _keepalive_loop(client: BleakClient) -> None:
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        try:
            await client.write_gatt_char(constants.CHAR_SYNC_RX_WRITE, KEEPALIVE_WRITE, response=False)
            logger.debug("sent keepalive")
        except Exception:
            logger.exception("keepalive write failed")
            return


async def run() -> None:
    device = await find_device()
    logger.info("Found %s (%s)", device.name, device.address)

    async with BleakClient(device) as client:
        notify_chars = []
        for service in client.services:
            for char in service.characteristics:
                props = set(char.properties)
                if "notify" in props or "indicate" in props:
                    notify_chars.append(char.uuid)

        logger.info("Subscribing to %d notify/indicate characteristics", len(notify_chars))
        for uuid in notify_chars:
            def make_handler(source_uuid: str):
                def handler(_char, data: bytearray) -> None:
                    _log_frame(source_uuid, data)
                return handler

            await client.start_notify(uuid, make_handler(uuid))

        await asyncio.sleep(0.5)

        for i, payload in enumerate(ACTIVATION_WRITES, start=1):
            logger.info("Activation write %d/%d: %s", i, len(ACTIVATION_WRITES), payload.hex())
            await client.write_gatt_char(constants.CHAR_SYNC_RX_WRITE, payload, response=False)
            await asyncio.sleep(0.3)

        keepalive_task = asyncio.create_task(_keepalive_loop(client))

        logger.info("Listening for %.0fs -- PRESS BOTH PADDLES REPEATEDLY NOW", LISTEN_SECONDS)
        start = time.monotonic()
        while time.monotonic() - start < LISTEN_SECONDS:
            await asyncio.sleep(1.0)

        keepalive_task.cancel()
        logger.info("Done listening.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
