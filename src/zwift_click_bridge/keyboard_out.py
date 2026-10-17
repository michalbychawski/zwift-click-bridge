from __future__ import annotations

from pynput.keyboard import Controller, Key

_SPECIAL_KEYS = {k.name: k for k in Key}


class KeyInjector:
    def __init__(self):
        self._controller = Controller()

    def _resolve(self, key_name: str):
        if len(key_name) == 1:
            return key_name
        try:
            return _SPECIAL_KEYS[key_name.lower()]
        except KeyError as exc:
            raise ValueError(f"unknown key name: {key_name!r}") from exc

    def tap(self, key_name: str) -> None:
        key = self._resolve(key_name)
        self._controller.press(key)
        self._controller.release(key)
