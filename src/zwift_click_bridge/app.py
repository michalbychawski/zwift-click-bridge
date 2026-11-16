from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import platform
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from . import __version__, logview
from .ble_client import DeviceScanner, ZwiftClickClient
from .gui import MainWindow
from .keyboard_out import KeyInjector
from .keymap import DEFAULT_CONFIG_PATH, KeyMap

logger = logging.getLogger(__name__)
LOG_PATH = DEFAULT_CONFIG_PATH.parent / "bridge.log"

STATUS_CONNECTING = "connecting"
STATUS_WAITING = "waiting"  # asleep after a drop, with a connection attempt left open for its next press
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
ALL_RELEASED = 0xFFFFFFFF
# Seconds after a disconnect at which "no advertisement yet" is logged, to tell a pod that
# went quiet from one the scanner just isn't reporting.
SILENCE_LOG_AFTER = (5, 15, 30, 60, 120, 300)
# A failed connect is retried at once while the pod was seen advertising this recently.
ADVERTISING_RETRY_SECONDS = 30.0
# Advertisements further apart than this count as the pod starting to advertise again.
ADVERTISING_GAP_SECONDS = 5.0
# What a connect to a sleeping pod ends in; expected while an attempt is left open for it.
_NOT_REACHABLE = (TimeoutError, asyncio.TimeoutError, BleakError)


@dataclass
class _Session:
    """Tracks one physical Click device across its whole connect/disconnect lifecycle."""

    address: str
    name: str
    task: asyncio.Task | None = None
    last_bitmask: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    last_button: float | None = None  # last button frame, unlike last_activity not reset by a connect
    connected: bool = False
    cooldown_until: float | None = None
    disconnect_requested: bool = False
    last_seen: float | None = None  # last matching advertisement
    advertising_since: float | None = None  # first advertisement of the current run of them
    disconnected_at: float | None = None
    silence_logged: int = 0  # how many of SILENCE_LOG_AFTER have been logged since the disconnect


def _noop(*_args) -> None:
    pass


class BridgeApp:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.keymap = KeyMap.load()
        # Both invoked on the asyncio thread; the GUI marshals them onto its own.
        self.device_status_changed: Callable[[str, str, str], None] = _noop  # address, name, status
        self.button_event: Callable[[str, str, bool], None] = _noop  # address, button_name, pressed
        self.injector = KeyInjector()
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._listening = True
        self._sessions: dict[str, _Session] = {}
        self._scanner = DeviceScanner(self._on_device_seen, side=self.keymap.controller_side)
        # Serializes the connect sequence (subscribe + activation writes) across devices --
        # two connecting at once on the shared BLE stack was observed to make one of them
        # silently stop receiving button notifications after the first press.
        self._connect_lock = asyncio.Lock()

    def _threadsafe(self, fn) -> None:
        self._loop.call_soon_threadsafe(fn)

    # -- controls, all called from the GUI thread --

    def request_stop(self) -> None:
        self._threadsafe(self._do_stop)

    def set_listening(self, enabled: bool) -> None:
        self._threadsafe(lambda: self._set_listening(enabled))

    def disconnect_device(self, address: str) -> None:
        self._threadsafe(lambda: self._disconnect_device(address))

    def save_keymap(self) -> None:
        # The mapping objects themselves are already updated in place by the GUI;
        # _on_button reads them live, so this only needs to persist them.
        try:
            self.keymap.save()
        except OSError:
            logger.exception("failed to save config to %s", self.keymap.path)

    # -- the above, actually applied on the asyncio loop thread --

    def _do_stop(self) -> None:
        self._stop_event.set()
        self._listening = False
        for session in self._sessions.values():
            session.disconnect_requested = True
        asyncio.create_task(self._scanner.stop())

    def _set_listening(self, enabled: bool) -> None:
        self._listening = enabled
        if enabled:
            asyncio.create_task(self._scanner.start())
        else:
            asyncio.create_task(self._scanner.stop())
            for session in self._sessions.values():
                session.disconnect_requested = True

    def _disconnect_device(self, address: str) -> None:
        session = self._sessions.get(address)
        if session is not None:
            session.disconnect_requested = True

    # -- discovery + per-device session lifecycle, all on the asyncio loop thread --

    def _on_device_seen(self, device: BLEDevice) -> None:
        address = device.address
        session = self._sessions.get(address)
        if session is None:
            session = _Session(address=address, name=f"{device.name or 'Zwift Click'} ({address[-5:]})")
            self._sessions[address] = session
            self.device_status_changed(address, session.name, STATUS_DISCONNECTED)
        now = time.monotonic()
        advertising_again = session.last_seen is None or now - session.last_seen > ADVERTISING_GAP_SECONDS
        if advertising_again:
            session.advertising_since = now
        session.last_seen = now

        if not self._listening:
            return
        if session.task is not None and not session.task.done():
            # Already connecting/connected. While an attempt is open for a sleeping pod, note
            # when it shows up, to tell from the log how long the connect took from here.
            if advertising_again and not session.connected and session.disconnected_at is not None:
                logger.info(
                    "%s: advertising again %.1fs after the last disconnect, a connection attempt is already open",
                    session.name,
                    now - session.disconnected_at,
                )
            return
        if session.cooldown_until is not None and time.monotonic() < session.cooldown_until:
            return  # recently disconnected on purpose -- ignore its lingering advertisement

        if session.disconnected_at is not None:
            logger.info(
                "%s: seen, connecting (%.1fs after the last disconnect)",
                session.name,
                time.monotonic() - session.disconnected_at,
            )
        else:
            logger.info("%s: seen, connecting", session.name)
        session.task = asyncio.create_task(self._run_session(device, session))

    def _on_button(self, session: _Session, bitmask: int) -> None:
        previous = session.last_bitmask
        session.last_bitmask = bitmask
        session.last_activity = session.last_button = time.monotonic()
        if previous is None:
            previous = ALL_RELEASED  # the first frame after a connect can be the press itself

        for bit, mapping in self.keymap.buttons.items():
            was_pressed = (previous & bit) == 0
            is_pressed = (bitmask & bit) == 0
            if is_pressed != was_pressed:
                self.button_event(session.address, mapping.name, is_pressed)
                if is_pressed and mapping.key:
                    self.injector.tap(mapping.key)
                    logger.info("%s: %s -> %s", session.name, mapping.name, mapping.key)

    def _keep_trying(self, session: _Session) -> bool:
        """After a drop or a failed connect: go straight back to connecting, or wait for an advertisement?"""
        if self._stop_event.is_set() or not self._listening or session.disconnect_requested:
            return False
        now = time.monotonic()
        if session.last_seen is not None and now - session.last_seen < ADVERTISING_RETRY_SECONDS:
            return True  # awake and advertising
        return (
            session.last_button is not None
            and now - session.last_button < self.keymap.standing_reconnect_seconds
        )

    async def _run_session(self, device: BLEDevice, session: _Session) -> None:
        session.disconnect_requested = False  # may be left over from Stop Listening while it wasn't connected
        standing = False
        while True:
            client = await self._connect(device, session, standing)
            if client is None:
                self.device_status_changed(session.address, session.name, STATUS_DISCONNECTED)
                return
            lost = await self._stay_connected(client, session)
            if not lost or not self._keep_trying(session):
                if lost:
                    logger.info("%s: reconnecting when it advertises again", session.name)
                return
            # The pod sleeps about a minute after the last press and only a press wakes it. With
            # a connect already open, that press doesn't first have to reach the scanner, and
            # an attempt that fails (the pod also advertises ~50 s after a drop without being
            # reachable) no longer leaves the bridge idle until the next advertisement.
            standing = True
            logger.info("%s: leaving a connection attempt open for its next press", session.name)

    async def _connect(self, device: BLEDevice, session: _Session, standing: bool) -> ZwiftClickClient | None:
        """Tries until the Click is connected. None once it's time to wait for an advertisement instead."""
        self.device_status_changed(
            session.address, session.name, STATUS_WAITING if standing else STATUS_CONNECTING
        )
        first_started = time.monotonic()
        failures = 0
        while True:
            client = ZwiftClickClient(
                device,
                lambda bitmask: self._on_button(session, bitmask),
                self.keymap.keepalive_mode,
                self.keymap.diagnostic_writes,
                self.keymap.keep_awake,
                quiet=standing or failures > 0,
            )
            started = time.monotonic()
            try:
                async with self._connect_lock:
                    await client.connect()
            except Exception as exc:
                failures += 1
                took = time.monotonic() - started
                if standing and isinstance(exc, _NOT_REACHABLE):
                    logger.debug("%s: attempt %d ended after %.1fs: %r", session.name, failures, took, exc)
                elif failures == 1:
                    logger.exception("%s: failed to connect after %.1fs", session.name, took)
                elif failures <= 3 or failures % 30 == 0:
                    logger.warning("%s: attempt %d failed after %.1fs: %r", session.name, failures, took, exc)
                try:  # a failure after the link came up (e.g. subscribing) would otherwise leave it open
                    await client.disconnect()
                except Exception:  # noqa: BLE001 - already failing, the next attempt starts fresh
                    logger.debug("disconnect after a failed connect failed", exc_info=True)
                if not standing:
                    standing = True  # from here on it's the same wait, whatever started it
                    self.device_status_changed(session.address, session.name, STATUS_WAITING)
            else:
                now = time.monotonic()
                if failures:
                    logger.info(
                        "%s: connected on attempt %d, %.0fs after the first",
                        session.name,
                        failures + 1,
                        now - first_started,
                    )
                since = session.advertising_since
                if since is not None and since >= (session.disconnected_at or 0.0):
                    logger.info("%s: ready %.1fs after it started advertising", session.name, now - since)
                return client

            if not self._keep_trying(session):
                if session.disconnect_requested and self._listening:  # the Disconnect button
                    logger.info("%s: giving up at your request", session.name)
                    session.cooldown_until = time.monotonic() + self.keymap.post_idle_cooldown_seconds
                    session.disconnect_requested = False
                else:
                    logger.info(
                        "%s: not connected after %d attempt(s) in %.0fs, waiting for it to advertise",
                        session.name,
                        failures,
                        time.monotonic() - first_started,
                    )
                return None
            if took < 1.0:
                await asyncio.sleep(1.0)  # a refusal that comes back at once must not spin

    async def _stay_connected(self, client: ZwiftClickClient, session: _Session) -> bool:
        """Runs until the connection ends. True if it was lost rather than closed by the bridge."""
        self.device_status_changed(session.address, session.name, STATUS_CONNECTED)
        session.connected = True
        session.last_bitmask = None
        session.last_activity = time.monotonic()
        lost = False
        try:
            while client.is_connected and not self._stop_event.is_set() and self._listening:
                if session.disconnect_requested:
                    # The Click keeps advertising for a while after any disconnect, so
                    # without this cooldown the scanner would just grab that lingering
                    # advertisement and immediately reconnect on its own -- defeating
                    # the point of an explicit "stay disconnected" button.
                    logger.info("%s: disconnecting at your request", session.name)
                    session.cooldown_until = time.monotonic() + self.keymap.post_idle_cooldown_seconds
                    break

                if time.monotonic() - session.last_activity >= self.keymap.idle_disconnect_seconds:
                    logger.info(
                        "%s: no button activity for %ss, disconnecting so it can sleep",
                        session.name,
                        self.keymap.idle_disconnect_seconds,
                    )
                    session.cooldown_until = time.monotonic() + self.keymap.post_idle_cooldown_seconds
                    break

                await asyncio.sleep(1.0)
            else:
                if not client.is_connected:
                    lost = True
                    logger.warning(
                        "%s: connection lost (last button activity %.0fs ago)",
                        session.name,
                        time.monotonic() - session.last_activity,
                    )
        finally:
            session.disconnect_requested = False
            session.connected = False
            await client.disconnect()
            session.disconnected_at = time.monotonic()
            session.silence_logged = 0
            self.device_status_changed(session.address, session.name, STATUS_DISCONNECTED)
        return lost

    async def _log_silence(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for session in self._sessions.values():
                if session.disconnected_at is None or session.connected:
                    continue
                if session.last_seen is not None and session.last_seen > session.disconnected_at:
                    continue
                if session.silence_logged >= len(SILENCE_LOG_AFTER):
                    continue
                if now - session.disconnected_at >= SILENCE_LOG_AFTER[session.silence_logged]:
                    session.silence_logged += 1
                    logger.info(
                        "%s: no advertisement for %.0fs since the disconnect", session.name, now - session.disconnected_at
                    )

    async def run(self) -> None:
        await self._scanner.start()
        silence_task = asyncio.create_task(self._log_silence())
        await self._stop_event.wait()
        silence_task.cancel()
        await self._scanner.stop()
        # Give each session a moment to close its connection itself (which also resets a pod
        # kept awake, see ZwiftClickClient.disconnect) before cancelling what's left.
        tasks = [s.task for s in self._sessions.values() if s.task is not None and not s.task.done()]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=3.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=2.0)


def _setup_logging() -> None:
    """Console, plus a small rotating file beside the config: the Windows .exe is built
    --windowed, so without the file its log (connects, drops, keepalive errors) is lost."""
    fmt = "%(asctime)s %(levelname)s %(message)s"
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    try:
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    # The window's log panel and its Copy button.
    handlers.append(logview.buffer)
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    try:
        bleak_version = importlib.metadata.version("bleak")
    except importlib.metadata.PackageNotFoundError:
        bleak_version = "unknown"
    logger.info(
        "Zwift Click Bridge %s on %s (Python %s, bleak %s); log file %s",
        __version__,
        platform.platform(),
        platform.python_version(),
        bleak_version,
        LOG_PATH,
    )


def main() -> None:
    _setup_logging()

    loop = asyncio.new_event_loop()
    bridge = BridgeApp(loop)

    window = MainWindow(
        bridge.keymap.buttons,
        on_toggle_listening=bridge.set_listening,
        on_disconnect_device=bridge.disconnect_device,
        on_mapping_changed=bridge.save_keymap,
        on_close=bridge.request_stop,
    )
    bridge.device_status_changed = window.on_device_status_changed
    bridge.button_event = window.on_button_event

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bridge.run())

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    window.run()
    thread.join(timeout=6.0)  # let bridge.run() close the connection; it's a daemon, so don't wait forever


if __name__ == "__main__":
    main()
