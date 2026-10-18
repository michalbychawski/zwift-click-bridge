from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import constants
from .protocol import decode_button_bitmask

logger = logging.getLogger(__name__)

ButtonHandler = Callable[[int], None]
DeviceSeenHandler = Callable[[BLEDevice], None]


def _matches(device: BLEDevice, adv) -> bool:
    name = device.name or ""
    if any(hint.lower() in name.lower() for hint in constants.DEVICE_NAME_HINTS):
        return True
    return constants.SERVICE_UUID.lower() in [u.lower() for u in adv.service_uuids]


async def find_device(timeout: float = 10.0) -> BLEDevice:
    device = await BleakScanner.find_device_by_filter(_matches, timeout=timeout)
    if device is None:
        raise RuntimeError("Zwift Click not found -- is it powered on and in range?")
    return device


class DeviceScanner:
    """Continuously scans for Click-matching devices, invoking on_device_seen for every
    matching advertisement -- including repeats of an already-known device, since that's
    how callers notice a previously-connected controller waking back up."""

    def __init__(self, on_device_seen: DeviceSeenHandler):
        self._on_device_seen = on_device_seen
        self._scanner: BleakScanner | None = None

    def _detection_callback(self, device: BLEDevice, advertisement_data) -> None:
        if _matches(device, advertisement_data):
            self._on_device_seen(device)

    async def start(self) -> None:
        if self._scanner is not None:
            return
        self._scanner = BleakScanner(detection_callback=self._detection_callback)
        await self._scanner.start()

    async def stop(self) -> None:
        if self._scanner is None:
            return
        scanner, self._scanner = self._scanner, None
        await scanner.stop()


class ZwiftClickClient:
    def __init__(self, device: BLEDevice, on_button: ButtonHandler):
        self._device = device
        self._on_button = on_button
        self._client: BleakClient | None = None
        self._keepalive_task: asyncio.Task | None = None

    def _on_notify(self, _char, data: bytearray) -> None:
        bitmask = decode_button_bitmask(bytes(data))
        if bitmask is not None:
            self._on_button(bitmask)

    async def _keepalive_loop(self) -> None:
        assert self._client is not None
        while True:
            await asyncio.sleep(constants.KEEPALIVE_INTERVAL_SECONDS)
            await self._client.write_gatt_char(constants.CHAR_SYNC_RX_WRITE, constants.KEEPALIVE_WRITE, response=False)

    async def connect(self) -> None:
        self._client = BleakClient(self._device)
        await self._client.connect()

        await self._client.start_notify(constants.CHAR_ASYNC_NOTIFY, self._on_notify)
        # Some units need a moment for the notify subscription to actually settle before
        # the activation writes land -- skipping this makes exactly one button press
        # register and then the notify stream silently goes dead for the rest of the
        # connection (confirmed empirically: this delay was the only variable that fixed it).
        await asyncio.sleep(0.5)

        for payload in constants.ACTIVATION_WRITES:
            await self._client.write_gatt_char(constants.CHAR_SYNC_RX_WRITE, payload, response=False)
            await asyncio.sleep(0.3)

        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info("Zwift Click connected and activated")

    async def disconnect(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        if self._client is not None:
            await self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)
