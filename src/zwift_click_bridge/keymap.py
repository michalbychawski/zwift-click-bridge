from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import constants

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zwift-click-bridge" / "config.json"

DEFAULT_CONFIG = {
    "reconnect_delay_seconds": 3,
    "buttons": {
        hex(constants.BUTTON_BIT_SHIFT_UP): {"name": "shift_up", "key": "i"},
        hex(constants.BUTTON_BIT_SHIFT_DOWN): {"name": "shift_down", "key": "k"},
    },
}


@dataclass
class ButtonMapping:
    name: str
    key: str


@dataclass
class KeyMap:
    reconnect_delay_seconds: float
    buttons: dict[int, ButtonMapping]

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "KeyMap":
        if path.exists():
            raw = json.loads(path.read_text())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
            raw = DEFAULT_CONFIG

        buttons = {
            int(bit, 16) if bit.lower().startswith("0x") else int(bit): ButtonMapping(**mapping)
            for bit, mapping in raw["buttons"].items()
        }
        return cls(
            reconnect_delay_seconds=raw["reconnect_delay_seconds"],
            buttons=buttons,
        )
