from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import constants

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zwift-click-bridge" / "config.json"

# The user's choice (2026-10-28): "shift_up" sends K and "b" sends I; "shift_down"
# stays on K. The rest default to unmapped (key: null).
_DEFAULT_KEYS = {
    constants.BUTTON_BIT_SHIFT_UP: "k",
    constants.BUTTON_BIT_SHIFT_DOWN: "k",
    0x20: "i",  # "b"
}

# Lets the Click drop into its own battery-saving sleep when unused, rather
# than the keepalive holding the connection open indefinitely.
DEFAULT_IDLE_DISCONNECT_SECONDS = 600

# After we disconnect it intentionally, the Click keeps advertising (and
# reconnecting) for roughly a minute before it actually sleeps -- observed
# empirically, not documented anywhere. Retrying sooner than this just
# catches that lingering advertisement and never lets it fully sleep.
DEFAULT_POST_IDLE_COOLDOWN_SECONDS = 90

# Only one Click v2 pod is connected -- see constants.DEVICE_TYPE_BY_SIDE.
DEFAULT_CONTROLLER_SIDE = "right"

# What the keepalive sends -- see constants.KEEPALIVE_GETS.
DEFAULT_KEEPALIVE_MODE = "rideon+get"

DEFAULT_CONFIG = {
    "controller_side": DEFAULT_CONTROLLER_SIDE,
    "reconnect_delay_seconds": 3,
    "idle_disconnect_seconds": DEFAULT_IDLE_DISCONNECT_SECONDS,
    "post_idle_cooldown_seconds": DEFAULT_POST_IDLE_COOLDOWN_SECONDS,
    "keepalive_mode": DEFAULT_KEEPALIVE_MODE,
    "buttons": {
        hex(bit): {"name": name, "key": _DEFAULT_KEYS.get(bit)}
        for bit, name in constants.BUTTON_BITS.items()
    },
}


@dataclass
class ButtonMapping:
    name: str
    key: str | None


@dataclass
class KeyMap:
    controller_side: str
    reconnect_delay_seconds: float
    idle_disconnect_seconds: float
    post_idle_cooldown_seconds: float
    keepalive_mode: str
    buttons: dict[int, ButtonMapping]
    path: Path = DEFAULT_CONFIG_PATH

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
        side = str(raw.get("controller_side", DEFAULT_CONTROLLER_SIDE)).lower()
        mode = str(raw.get("keepalive_mode", DEFAULT_KEEPALIVE_MODE)).lower()
        return cls(
            controller_side=side if side in constants.DEVICE_TYPE_BY_SIDE else DEFAULT_CONTROLLER_SIDE,
            reconnect_delay_seconds=raw["reconnect_delay_seconds"],
            idle_disconnect_seconds=raw.get("idle_disconnect_seconds", DEFAULT_IDLE_DISCONNECT_SECONDS),
            post_idle_cooldown_seconds=raw.get("post_idle_cooldown_seconds", DEFAULT_POST_IDLE_COOLDOWN_SECONDS),
            keepalive_mode=mode if mode in constants.KEEPALIVE_MODES else DEFAULT_KEEPALIVE_MODE,
            buttons=buttons,
            path=path,
        )

    def save(self) -> None:
        raw = {
            "controller_side": self.controller_side,
            "reconnect_delay_seconds": self.reconnect_delay_seconds,
            "idle_disconnect_seconds": self.idle_disconnect_seconds,
            "post_idle_cooldown_seconds": self.post_idle_cooldown_seconds,
            "keepalive_mode": self.keepalive_mode,
            "buttons": {
                hex(bit): {"name": mapping.name, "key": mapping.key}
                for bit, mapping in self.buttons.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(raw, indent=2))
