from __future__ import annotations

from collections.abc import Callable

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem


def _make_icon(color: str) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return image


class TrayApp:
    def __init__(self, on_quit: Callable[[], None]):
        self._on_quit = on_quit
        self._icon = Icon(
            "zwift-click-bridge",
            icon=_make_icon("gray"),
            title="Zwift Click Bridge (disconnected)",
            menu=Menu(MenuItem("Quit", self._quit)),
        )

    def _quit(self, *_args) -> None:
        self._on_quit()
        self._icon.stop()

    def set_connected(self, connected: bool) -> None:
        self._icon.icon = _make_icon("green" if connected else "gray")
        self._icon.title = f"Zwift Click Bridge ({'connected' if connected else 'disconnected'})"

    def run(self) -> None:
        self._icon.run()
