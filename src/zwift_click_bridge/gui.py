from __future__ import annotations

import queue
import tkinter as tk
from collections.abc import Callable

from .keyboard_out import is_valid_key
from .keymap import ButtonMapping

_STATUS_STYLE = {
    "disconnected": ("#999999", "Disconnected"),
    "connecting": ("#e0b000", "Connecting..."),
    "connected": ("#2ecc71", "Connected"),
}

_BG = "#1e1e1e"
_PANEL_BG = "#262626"
_TILE_BG = "#333333"
_TILE_BORDER = "#4a4a4a"
_FLASH_BG = "#2ecc71"
_CAPTURE_BG = "#2d6cdf"

_FLASH_MS = 180
_POLL_MS = 20
_INDICATOR_COLUMNS = 4

# Tk keysyms whose pynput Key name differs from just lowercasing them.
_TK_KEYSYM_TO_PYNPUT = {
    "Return": "enter",
    "KP_Enter": "enter",
    "Escape": "esc",
    "Prior": "page_up",
    "Next": "page_down",
    "BackSpace": "backspace",
    "Caps_Lock": "caps_lock",
    "Num_Lock": "num_lock",
    "Scroll_Lock": "scroll_lock",
    "Print": "print_screen",
    "App": "menu",
    "Control_L": "ctrl_l",
    "Control_R": "ctrl_r",
    "Alt_L": "alt_l",
    "Alt_R": "alt_r",
    "Shift_L": "shift_l",
    "Shift_R": "shift_r",
    "Super_L": "cmd",
    "Super_R": "cmd_r",
    "Win_L": "cmd",
    "Win_R": "cmd_r",
}


def _key_from_event(event: tk.Event) -> str | None:
    """Turns a Tk key press into the key name format KeyInjector understands."""
    if event.keysym == "space":
        return "space"
    if len(event.char) == 1 and event.char.isprintable():
        return event.char.lower()
    name = _TK_KEYSYM_TO_PYNPUT.get(event.keysym, event.keysym.lower())
    return name if is_valid_key(name) else None


class ButtonTile(tk.Frame):
    """Shows one button's mapping and flashes on press. Left-click it, then press a key
    to remap; right-click to unmap."""

    def __init__(self, parent: tk.Widget, mapping: ButtonMapping, on_click: Callable, on_clear: Callable):
        super().__init__(parent, bg=_TILE_BG, highlightthickness=1, highlightbackground=_TILE_BORDER,
                         width=96, height=54, cursor="hand2")
        self.grid_propagate(False)
        self.pack_propagate(False)
        self.mapping = mapping
        self._flash_job: str | None = None
        self._capturing = False

        self._name_label = tk.Label(self, text=mapping.name.replace("_", " "), bg=_TILE_BG, fg="white",
                                    font=("Segoe UI", 10, "bold"))
        self._name_label.pack(pady=(6, 0))
        self._key_label = tk.Label(self, bg=_TILE_BG, fg="#aaaaaa", font=("Segoe UI", 9))
        self._key_label.pack()
        self.refresh()

        for widget in (self, self._name_label, self._key_label):
            widget.bind("<Button-1>", lambda _e: on_click(self))
            widget.bind("<Button-3>", lambda _e: on_clear(self))

    def _set_bg(self, color: str) -> None:
        for widget in (self, self._name_label, self._key_label):
            widget.configure(bg=color)

    def refresh(self) -> None:
        if self._capturing:
            self._key_label.configure(text="press a key...")
        else:
            self._key_label.configure(text=f"-> {self.mapping.key}" if self.mapping.key else "unmapped")

    def set_capturing(self, capturing: bool) -> None:
        self._capturing = capturing
        self._set_bg(_CAPTURE_BG if capturing else _TILE_BG)
        self.refresh()

    def flash(self) -> None:
        if self._capturing:
            return
        self._set_bg(_FLASH_BG)
        if self._flash_job is not None:
            self.after_cancel(self._flash_job)
        self._flash_job = self.after(_FLASH_MS, self._revert)

    def _revert(self) -> None:
        self._flash_job = None
        if not self._capturing:
            self._set_bg(_TILE_BG)


class MainWindow:
    """The bridge's callbacks fire on the asyncio thread; Tk isn't thread-safe, so
    they're queued here and drained on the Tk thread by a polling timer."""

    def __init__(
        self,
        buttons: dict[int, ButtonMapping],
        on_toggle_listening: Callable[[bool], None],
        on_disconnect_device: Callable[[str], None],
        on_mapping_changed: Callable[[], None],
        on_close: Callable[[], None],
    ):
        self._on_toggle_listening = on_toggle_listening
        self._on_disconnect_device = on_disconnect_device
        self._on_mapping_changed = on_mapping_changed
        self._on_close = on_close
        self._listening = True
        self._events: queue.Queue = queue.Queue()
        self._address: str | None = None
        self._capturing: ButtonTile | None = None

        self.root = tk.Tk()
        self.root.title("Zwift Click Bridge")
        self.root.configure(bg=_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.root.bind("<KeyPress>", self._handle_key)

        panel = tk.Frame(self.root, bg=_PANEL_BG, padx=10, pady=10)
        panel.pack(fill="both", expand=True, padx=10, pady=(10, 6))

        header = tk.Frame(panel, bg=_PANEL_BG)
        header.pack(fill="x", pady=(0, 8))
        self._dot = tk.Canvas(header, width=14, height=14, bg=_PANEL_BG, highlightthickness=0)
        self._dot_item = self._dot.create_oval(1, 1, 13, 13, outline="")
        self._dot.pack(side="left", padx=(0, 6))
        self._name_label = tk.Label(header, text="Waiting for the right Click -- press a paddle to wake it.",
                                    bg=_PANEL_BG, fg="white", font=("Segoe UI", 11, "bold"))
        self._name_label.pack(side="left")
        self._status_label = tk.Label(header, bg=_PANEL_BG, fg="#aaaaaa")
        self._status_label.pack(side="left", padx=6)
        self._disconnect_button = tk.Button(header, text="Disconnect", state="disabled",
                                            command=self._handle_disconnect)
        self._disconnect_button.pack(side="right")

        grid = tk.Frame(panel, bg=_PANEL_BG)
        grid.pack()
        self._tiles: dict[str, ButtonTile] = {}
        for i, mapping in enumerate(buttons.values()):
            tile = ButtonTile(grid, mapping, self._start_capture, self._clear_mapping)
            tile.grid(row=i // _INDICATOR_COLUMNS, column=i % _INDICATOR_COLUMNS, padx=3, pady=3)
            self._tiles[mapping.name] = tile

        self._hint_label = tk.Label(panel, bg=_PANEL_BG, fg="#888888")
        self._hint_label.pack(pady=(8, 0))
        self._show_default_hint()

        self._toggle_button = tk.Button(self.root, text="Stop Listening", command=self._handle_toggle)
        self._toggle_button.pack(fill="x", padx=10, pady=(0, 10))

        self._set_status("disconnected")
        self.root.after(_POLL_MS, self._drain_events)

    def run(self) -> None:
        self.root.mainloop()

    # -- thread-safe entry points, called from the asyncio thread --

    def on_device_status_changed(self, address: str, name: str, status: str) -> None:
        self._events.put(("status", address, name, status))

    def on_button_event(self, address: str, name: str, pressed: bool) -> None:
        if pressed:
            self._events.put(("button", address, name))

    # -- everything below runs on the Tk thread --

    def _drain_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "status":
                _, address, name, status = event
                self._address = address
                self._name_label.configure(text=name)
                self._set_status(status)
            elif event[0] == "button":
                tile = self._tiles.get(event[2])
                if tile is not None:
                    tile.flash()
        self.root.after(_POLL_MS, self._drain_events)

    def _set_status(self, status: str) -> None:
        color, text = _STATUS_STYLE.get(status, ("#999999", status))
        self._dot.itemconfigure(self._dot_item, fill=color)
        self._status_label.configure(text=text)
        self._disconnect_button.configure(state="normal" if status == "connected" else "disabled")

    def _show_default_hint(self) -> None:
        self._hint_label.configure(text="Click a button to remap it, right-click to unmap.")

    def _start_capture(self, tile: ButtonTile) -> None:
        if self._capturing is not None:
            self._capturing.set_capturing(False)
        if self._capturing is tile:
            self._capturing = None
            self._show_default_hint()
            return
        self._capturing = tile
        tile.set_capturing(True)
        self._hint_label.configure(text=f"Press the key for '{tile.mapping.name}' (Esc to cancel).")
        self.root.focus_force()

    def _stop_capture(self) -> None:
        if self._capturing is not None:
            self._capturing.set_capturing(False)
            self._capturing = None
        self._show_default_hint()

    def _handle_key(self, event: tk.Event) -> None:
        tile = self._capturing
        if tile is None:
            return
        if event.keysym == "Escape":
            self._stop_capture()
            return
        key = _key_from_event(event)
        if key is None:
            self._hint_label.configure(text=f"Can't map '{event.keysym}' -- try another key (Esc to cancel).")
            return
        tile.mapping.key = key
        self._stop_capture()
        self._on_mapping_changed()

    def _clear_mapping(self, tile: ButtonTile) -> None:
        if self._capturing is tile:
            self._stop_capture()
        tile.mapping.key = None
        tile.refresh()
        self._on_mapping_changed()

    def _handle_disconnect(self) -> None:
        if self._address is not None:
            self._on_disconnect_device(self._address)

    def _handle_toggle(self) -> None:
        self._listening = not self._listening
        self._toggle_button.configure(text="Stop Listening" if self._listening else "Start Listening")
        self._on_toggle_listening(self._listening)

    def _handle_close(self) -> None:
        self._on_close()
        self.root.destroy()
