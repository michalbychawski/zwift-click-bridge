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

from . import __version__, logview
from .ble_client import DeviceScanner, ZwiftClickClient
from .gui import MainWindow
from .keyboard_out import KeyInjector
from .keymap import DEFAULT_CONFIG_PATH, KeyMap

logger = logging.getLogger(__name__)
LOG_PATH = DEFAULT_CONFIG_PATH.parent / "bridge.log"

STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
ALL_RELEASED = 0xFFFFFFFF


@dataclass
class _Session:
    """Tracks one physical Click device across its whole connect/disconnect lifecycle."""

    address: str
    name: str
    task: asyncio.Task | None = None
    last_bitmask: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    connected: bool = False
    cooldown_until: float | None = None
    disconnect_requested: bool = False
    disconnected_at: float | None = None


def _noop(*_args) -> None:
    pass


class BridgeApp:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.keymap = KeyMap.load()
        # All invoked on the asyncio thread; the GUI marshals them onto its own.
        self.device_status_changed: Callable[[str, str, str], None] = _noop  # address, name, status
        self.button_event: Callable[[str, str, bool], None] = _noop  # address, button_name, pressed
        self.device_info: Callable[[str, str, object], None] = _noop  # address, what, value (see ZwiftClickClient)
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

        if not self._listening:
            return
        if session.task is not None and not session.task.done():
            return  # already connecting/connected
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
        session.last_activity = time.monotonic()
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

    async def _run_session(self, device: BLEDevice, session: _Session) -> None:
        """One connection. After it ends (or fails), the next advertisement starts a new one."""
        session.disconnect_requested = False  # may be left over from Stop Listening while it wasn't connected
        self.device_status_changed(session.address, session.name, STATUS_CONNECTING)
        client = ZwiftClickClient(
            device,
            lambda bitmask: self._on_button(session, bitmask),
            self.keymap.keep_awake,
            on_info=lambda what, value: self.device_info(session.address, what, value),
        )
        started = time.monotonic()
        try:
            async with self._connect_lock:
                await client.connect()
        except Exception:
            logger.exception("%s: failed to connect after %.1fs", session.name, time.monotonic() - started)
            try:  # a failure after the link came up (e.g. subscribing) would otherwise leave it open
                await client.disconnect()
            except Exception:  # noqa: BLE001 - already failing, the next attempt starts fresh
                logger.debug("disconnect after a failed connect failed", exc_info=True)
            session.disconnected_at = time.monotonic()
            self.device_status_changed(session.address, session.name, STATUS_DISCONNECTED)
            return
        if await self._stay_connected(client, session):
            logger.info("%s: reconnecting when it advertises again", session.name)

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
            self.device_status_changed(session.address, session.name, STATUS_DISCONNECTED)
        return lost

    async def run(self) -> None:
        await self._scanner.start()
        await self._stop_event.wait()
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
        controller_side=bridge.keymap.controller_side,
    )
    bridge.device_status_changed = window.on_device_status_changed
    bridge.button_event = window.on_button_event
    bridge.device_info = window.on_device_info

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bridge.run())

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    window.run()
    thread.join(timeout=6.0)  # let bridge.run() close the connection; it's a daemon, so don't wait forever


if __name__ == "__main__":
    main()
