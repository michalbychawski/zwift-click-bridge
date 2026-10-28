from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
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


def device_type(adv) -> int | None:
    """First byte of Zwift's manufacturer data, or None if this advertisement lacks it
    (it can be missing from some packets, so callers just wait for the next one)."""
    data = adv.manufacturer_data.get(constants.ZWIFT_MANUFACTURER_ID)
    return data[0] if data else None


def side_matcher(side: str) -> Callable[[BLEDevice, object], bool]:
    wanted = constants.DEVICE_TYPE_BY_SIDE[side]
    return lambda device, adv: _matches(device, adv) and device_type(adv) == wanted


async def find_device(timeout: float = 10.0, side: str = "right") -> BLEDevice:
    device = await BleakScanner.find_device_by_filter(side_matcher(side), timeout=timeout)
    if device is None:
        raise RuntimeError("Zwift Click not found -- is it powered on and in range?")
    return device


class DeviceScanner:
    """Continuously scans for Click-matching devices, invoking on_device_seen for every
    matching advertisement -- including repeats of an already-known device, since that's
    how callers notice a previously-connected controller waking back up."""

    def __init__(self, on_device_seen: DeviceSeenHandler, side: str = "right"):
        self._on_device_seen = on_device_seen
        self._matches = side_matcher(side)
        self._scanner: BleakScanner | None = None
        self._logged_types: set[tuple[str, int | None]] = set()

    def _detection_callback(self, device: BLEDevice, advertisement_data) -> None:
        if not _matches(device, advertisement_data):
            return
        key = (device.address, device_type(advertisement_data))
        if key not in self._logged_types:
            self._logged_types.add(key)
            type_text = f"0x{key[1]:02x}" if key[1] is not None else "unknown"
            logger.info("Saw %s (%s), device type %s", device.name, device.address, type_text)
        if self._matches(device, advertisement_data):
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
    def __init__(self, device: BLEDevice, on_button: ButtonHandler, keepalive_mode: str = "rideon"):
        self._device = device
        self._on_button = on_button
        self._keepalive_mode = keepalive_mode
        self._reply_types: Counter[int] = Counter()
        self._client: BleakClient | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._connected_at: float | None = None
        self._keepalives_sent = 0
        self._sync_replies = 0
        self._last_reply_at: float | None = None
        self._notifications = 0
        self._other_notifications = 0
        self._last_notify_at: float | None = None

    def _reply_summary(self) -> str:
        return "{" + ", ".join(f"0x{op:02x}: {n}" for op, n in sorted(self._reply_types.items())) + "}"

    def _ago(self, at: float | None) -> str:
        return f"{time.monotonic() - at:.0f}s ago" if at is not None else "never"

    def _on_notify(self, _char, data: bytearray) -> None:
        raw = bytes(data)
        self._notifications += 1
        self._last_notify_at = time.monotonic()
        bitmask = decode_button_bitmask(raw)
        if bitmask is not None:
            logger.debug("button frame %s -> 0x%x", raw.hex(), bitmask)
            self._on_button(bitmask)
            return
        # Not a button frame (battery, status, a message before sleeping...): log the first
        # ones and then now and then, so a drop can be matched to what the Click sent.
        self._other_notifications += 1
        if self._other_notifications <= 20 or self._other_notifications % 50 == 0:
            logger.info("other notification #%d: %s", self._other_notifications, raw.hex())

    def _on_sync_tx(self, _char, data: bytearray) -> None:
        # The device answers activation and every keepalive here. Nothing to act on, but the
        # subscription has to exist: sniff.py, where the keepalive was found to keep the Click
        # awake, subscribed to every notify/indicate characteristic, and the bridge without
        # this subscription saw the Click drop about once a minute despite the keepalive.
        self._sync_replies += 1
        self._last_reply_at = time.monotonic()
        if data:
            self._reply_types[data[0]] += 1
        if self._sync_replies <= 5:
            logger.info("SYNC_TX reply #%d: %s", self._sync_replies, bytes(data).hex())
        else:
            logger.debug("SYNC_TX reply: %s", bytes(data).hex())

    def _on_disconnected(self, _client: BleakClient) -> None:
        # May run off the asyncio thread on some backends, so plain monotonic time.
        connected_for = time.monotonic() - self._connected_at if self._connected_at is not None else 0.0
        logger.warning(
            "Zwift Click disconnected after %.0fs (%d keepalives sent, %d replies, last reply %s; "
            "%d notifications, last %s)",
            connected_for,
            self._keepalives_sent,
            self._sync_replies,
            self._ago(self._last_reply_at),
            self._notifications,
            self._ago(self._last_notify_at),
        )

    async def _keepalive_loop(self) -> None:
        assert self._client is not None
        failures = 0
        ticks = 0
        while True:
            await asyncio.sleep(constants.KEEPALIVE_INTERVAL_SECONDS)
            ticks += 1
            # A failed write used to end this task silently (nothing awaits it), so the
            # keepalive stopped and the Click went idle and dropped about a minute later.
            writes = []
            if self._keepalive_mode in ("rideon", "rideon+get"):
                writes.append(constants.KEEPALIVE_WRITE)
            # "get": a request every tick; "rideon+get": one every other tick, beside the RideOn.
            if self._keepalive_mode == "get" or (self._keepalive_mode == "rideon+get" and ticks % 2 == 0):
                step = ticks if self._keepalive_mode == "get" else ticks // 2
                writes.append(constants.KEEPALIVE_GETS[step % len(constants.KEEPALIVE_GETS)])
            try:
                for payload in writes:
                    await self._client.write_gatt_char(constants.CHAR_SYNC_RX_WRITE, payload, response=False)
                self._keepalives_sent += 1
                if failures:
                    logger.info("keepalive writes work again after %d failure(s)", failures)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    logger.exception("keepalive write failed (%d in a row)", failures)
            if ticks % 10 == 0:  # every 30 s
                logger.info(
                    "still connected: %d keepalives sent (%s), %d replies %s (last %s), %d notifications (last %s)",
                    self._keepalives_sent,
                    self._keepalive_mode,
                    self._sync_replies,
                    self._reply_summary(),
                    self._ago(self._last_reply_at),
                    self._notifications,
                    self._ago(self._last_notify_at),
                )

    async def connect(self) -> None:
        started = time.monotonic()
        logger.info("connecting to %s (%s)", self._device.name, self._device.address)
        self._client = BleakClient(self._device, disconnected_callback=self._on_disconnected)
        await self._client.connect()
        self._connected_at = time.monotonic()
        try:
            mtu = self._client.mtu_size
        except Exception:  # noqa: BLE001 - not every backend knows it
            mtu = "?"
        logger.info("BLE connected in %.1fs, MTU %s", self._connected_at - started, mtu)
        try:
            for service in self._client.services:
                if service.uuid.lower() == constants.SERVICE_UUID.lower():
                    for char in service.characteristics:
                        logger.info("  characteristic %s: %s", char.uuid, ",".join(char.properties))
        except Exception:  # noqa: BLE001 - only a log line
            pass

        await self._client.start_notify(constants.CHAR_ASYNC_NOTIFY, self._on_notify)
        try:
            await self._client.start_notify(constants.CHAR_SYNC_TX_INDICATE, self._on_sync_tx)
            logger.info("subscribed to button notifications and SYNC_TX indications")
        except Exception:
            logger.exception("could not subscribe to SYNC_TX indications; continuing without them")
        # Some units need a moment for the notify subscription to actually settle before
        # the activation writes land -- skipping this makes exactly one button press
        # register and then the notify stream silently goes dead for the rest of the
        # connection (confirmed empirically: this delay was the only variable that fixed it).
        await asyncio.sleep(0.5)

        for payload in constants.ACTIVATION_WRITES:
            await self._client.write_gatt_char(constants.CHAR_SYNC_RX_WRITE, payload, response=False)
            await asyncio.sleep(0.3)

        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info("Zwift Click connected and activated (keepalive: %s)", self._keepalive_mode)

    async def disconnect(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        if self._client is not None:
            await self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)
