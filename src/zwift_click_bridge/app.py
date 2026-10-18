from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

from bleak.backends.device import BLEDevice
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from .ble_client import DeviceScanner, ZwiftClickClient
from .gui import MainWindow
from .keyboard_out import KeyInjector
from .keymap import KeyMap

logger = logging.getLogger(__name__)

STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"


@dataclass
class _Session:
    """Tracks one physical Click device across its whole connect/disconnect lifecycle."""

    address: str
    name: str
    task: asyncio.Task | None = None
    last_bitmask: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    cooldown_until: float | None = None
    disconnect_requested: bool = False


class BridgeApp(QObject):
    device_status_changed = Signal(str, str, str)  # address, name, status
    button_event = Signal(str, str, bool)  # address, button_name, pressed

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.keymap = KeyMap.load()
        self.injector = KeyInjector()
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._listening = True
        self._sessions: dict[str, _Session] = {}
        self._scanner = DeviceScanner(self._on_device_seen)
        # Serializes the connect sequence (subscribe + activation writes) across devices --
        # two connecting at once on the shared BLE stack was observed to make one of them
        # silently stop receiving button notifications after the first press.
        self._connect_lock = asyncio.Lock()

    def _threadsafe(self, fn) -> None:
        self._loop.call_soon_threadsafe(fn)

    # -- controls, all called from the Qt thread --

    def request_stop(self) -> None:
        self._threadsafe(self._do_stop)

    def set_listening(self, enabled: bool) -> None:
        self._threadsafe(lambda: self._set_listening(enabled))

    def disconnect_device(self, address: str) -> None:
        self._threadsafe(lambda: self._disconnect_device(address))

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
            self.device_status_changed.emit(address, session.name, STATUS_DISCONNECTED)

        if not self._listening:
            return
        if session.task is not None and not session.task.done():
            return  # already connecting/connected
        if session.cooldown_until is not None and time.monotonic() < session.cooldown_until:
            return  # recently disconnected on purpose -- ignore its lingering advertisement

        session.task = asyncio.create_task(self._run_session(device, session))

    def _on_button(self, session: _Session, bitmask: int) -> None:
        previous = session.last_bitmask
        session.last_bitmask = bitmask
        session.last_activity = time.monotonic()
        if previous is None:
            return  # first frame -- nothing to compare against yet

        for bit, mapping in self.keymap.buttons.items():
            was_pressed = (previous & bit) == 0
            is_pressed = (bitmask & bit) == 0
            if is_pressed != was_pressed:
                self.button_event.emit(session.address, mapping.name, is_pressed)
                if is_pressed and mapping.key:
                    self.injector.tap(mapping.key)
                    logger.info("%s: %s -> %s", session.name, mapping.name, mapping.key)

    async def _run_session(self, device: BLEDevice, session: _Session) -> None:
        self.device_status_changed.emit(session.address, session.name, STATUS_CONNECTING)
        client = ZwiftClickClient(device, lambda bitmask: self._on_button(session, bitmask))
        try:
            async with self._connect_lock:
                await client.connect()
        except Exception:
            logger.exception("%s: failed to connect", session.name)
            self.device_status_changed.emit(session.address, session.name, STATUS_DISCONNECTED)
            return

        self.device_status_changed.emit(session.address, session.name, STATUS_CONNECTED)
        session.last_bitmask = None
        session.last_activity = time.monotonic()
        session.disconnect_requested = False
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

                idle_for = time.monotonic() - session.last_activity
                if idle_for >= self.keymap.idle_disconnect_seconds:
                    logger.info(
                        "%s: no button activity for %ss, disconnecting so it can sleep",
                        session.name,
                        self.keymap.idle_disconnect_seconds,
                    )
                    session.cooldown_until = time.monotonic() + self.keymap.post_idle_cooldown_seconds
                    break

                await asyncio.sleep(1.0)
        finally:
            session.disconnect_requested = False
            await client.disconnect()
            self.device_status_changed.emit(session.address, session.name, STATUS_DISCONNECTED)

    async def run(self) -> None:
        await self._scanner.start()
        await self._stop_event.wait()
        await self._scanner.stop()
        for session in list(self._sessions.values()):
            if session.task is not None:
                session.task.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    qt_app = QApplication([])

    loop = asyncio.new_event_loop()
    bridge = BridgeApp(loop)

    window = MainWindow(
        bridge.keymap.buttons,
        on_toggle_listening=bridge.set_listening,
        on_disconnect_device=bridge.disconnect_device,
        on_close=bridge.request_stop,
    )
    bridge.device_status_changed.connect(window.on_device_status_changed)
    bridge.button_event.connect(window.on_button_event)

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bridge.run())

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    window.show()
    qt_app.exec()


if __name__ == "__main__":
    main()
