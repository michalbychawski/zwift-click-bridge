from __future__ import annotations

import asyncio
import logging
import threading

from .ble_client import ZwiftClickClient, find_device
from .keyboard_out import KeyInjector
from .keymap import KeyMap
from .tray import TrayApp

logger = logging.getLogger(__name__)


class BridgeApp:
    def __init__(self):
        self.keymap = KeyMap.load()
        self.injector = KeyInjector()
        self.tray = TrayApp(on_quit=self._request_stop)
        self._stop_event = asyncio.Event()
        self._last_bitmask: int | None = None

    def _request_stop(self) -> None:
        self._stop_event.set()

    def _on_button(self, bitmask: int) -> None:
        previous = self._last_bitmask
        self._last_bitmask = bitmask
        if previous is None:
            return  # first frame -- nothing to compare against yet

        for bit, mapping in self.keymap.buttons.items():
            was_pressed = (previous & bit) == 0
            is_pressed = (bitmask & bit) == 0
            if is_pressed and not was_pressed:
                self.injector.tap(mapping.key)
                logger.info("%s -> %s", mapping.name, mapping.key)

    async def _run_once(self) -> None:
        device = await find_device()
        client = ZwiftClickClient(device, self._on_button)
        await client.connect()
        self.tray.set_connected(True)
        try:
            while client.is_connected and not self._stop_event.is_set():
                await asyncio.sleep(1.0)
        finally:
            self.tray.set_connected(False)
            self._last_bitmask = None
            await client.disconnect()

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._run_once()
            except Exception:
                logger.exception("connection lost, retrying in %ss", self.keymap.reconnect_delay_seconds)
            if not self._stop_event.is_set():
                await asyncio.sleep(self.keymap.reconnect_delay_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = BridgeApp()

    loop = asyncio.new_event_loop()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(app.run())

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    app.tray.run()  # blocks the main thread -- required for tray icons on macOS


if __name__ == "__main__":
    main()
