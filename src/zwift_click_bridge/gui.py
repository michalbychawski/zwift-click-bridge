from __future__ import annotations

import queue
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

from . import __version__, logview
from .keyboard_out import is_valid_key
from .keymap import ButtonMapping

# -- look --

_BG = "#14161a"
_PANEL = "#1d2026"
_PANEL_LINE = "#2a2e36"
_TEXT = "#e8eaed"
_MUTED = "#8b919c"
_ACCENT = "#ff6a14"  # Zwift orange
_GOOD = "#2ecc71"
_WARN = "#e0b000"
_BAD = "#e5484d"
_CAPTURE = "#2d6cdf"
_FONT = "Segoe UI"

_STATUS_STYLE = {
    "disconnected": (_MUTED, "Not connected"),
    "connecting": (_WARN, "Connecting..."),
    "connected": (_GOOD, "Connected"),
}

_FLASH_MS = 220
_POLL_MS = 20
_LOG_POLL_MS = 300
_LOG_VIEW_LINES = 500  # kept in the panel; Copy takes the whole buffer (logview.MAX_LINES)

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


def _key_text(key: str | None) -> str:
    if not key:
        return "--"
    return key.upper() if len(key) == 1 else key.replace("_", " ")


def _mix(color: str, other: str, amount: float) -> str:
    """color moved `amount` (0-1) of the way towards other."""
    a = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


# -- the controller drawing --
# Both Click v2 pods, drawn after the real ones (the left one has a d-pad and the "-" paddle,
# the right one Y/Z/A/B and the "+" paddle). Shapes are in pod coordinates, centre (0, 0).

_CANVAS_W, _CANVAS_H = 760, 330
_POD_Y = 158
_POD_X = {"left": 190, "right": 570}

_BODY = [
    (0, -112), (48, -108), (84, -90), (102, -52), (108, -8), (100, 34), (80, 64), (64, 92),
    (58, 116), (36, 128), (-36, 128), (-58, 116), (-64, 92), (-80, 64), (-100, 34), (-108, -8),
    (-102, -52), (-84, -90), (-48, -108),
]
_EARS = [(-1, -1), (1, -1), (-1, 1), (1, 1)]  # the mount's clamp tabs at each corner

_FACE_COLORS = {"y": "#39c2dd", "z": "#ee7a3c", "a": "#3fb87a", "b": "#c8489e"}
_FACE_POS = {"y": (0, -50), "z": (-46, -6), "a": (46, -6), "b": (0, 38)}
_FACE_R = 23
_DPAD = {"up": (0, -52), "down": (0, 40), "left": (-46, -6), "right": (46, -6)}
_PADDLE = {"left": ("shift_down", "−"), "right": ("shift_up", "+")}


@dataclass
class _Part:
    """One pressable button on the drawing, and how to light it."""

    items: list[int]  # canvas items recoloured on a press
    base: list[str]
    lit: list[str]
    chip: int | None = None  # the key chip's background
    chip_text: int | None = None


class ControllerView(tk.Canvas):
    def __init__(self, parent: tk.Widget, buttons: dict[int, ButtonMapping], side: str,
                 on_click: Callable[[str], None], on_clear: Callable[[str], None]):
        super().__init__(parent, width=_CANVAS_W, height=_CANVAS_H, bg=_PANEL, highlightthickness=0)
        self._side = side
        self._on_click = on_click
        self._on_clear = on_clear
        self._mappings = {m.name: m for m in buttons.values()}
        self._parts: dict[str, _Part] = {}
        self._flash_jobs: dict[str, str] = {}
        self._capturing: str | None = None
        self._leds: dict[str, int] = {}
        for pod_side in ("left", "right"):
            self._draw_pod(pod_side)
        self._draw_chips()

    # coordinates

    def _xy(self, side: str, x: float, y: float) -> tuple[float, float]:
        return _POD_X[side] + x, _POD_Y + y

    def _poly(self, side: str, points, **kw) -> int:
        flat = [c for x, y in points for c in self._xy(side, x, y)]
        return self.create_polygon(*flat, smooth=True, splinesteps=24, **kw)

    def _oval(self, side: str, x: float, y: float, r: float, **kw) -> int:
        cx, cy = self._xy(side, x, y)
        return self.create_oval(cx - r, cy - r, cx + r, cy + r, **kw)

    def _shade(self, side: str, color: str) -> str:
        return color if side == self._side else _mix(color, _PANEL, 0.72)

    def _add_part(self, name: str, items: list[int], base: list[str], lit: list[str]) -> None:
        if name not in self._mappings:
            return
        self._parts[name] = _Part(items, base, lit)
        tag = f"btn:{name}"
        for item in items:
            self.addtag_withtag(tag, item)
        self.tag_bind(tag, "<Button-1>", lambda _e, n=name: self._on_click(n))
        self.tag_bind(tag, "<Button-3>", lambda _e, n=name: self._on_clear(n))
        self.tag_bind(tag, "<Enter>", lambda _e: self.configure(cursor="hand2"))
        self.tag_bind(tag, "<Leave>", lambda _e: self.configure(cursor=""))

    # drawing

    def _draw_pod(self, side: str) -> None:
        shade = lambda c: self._shade(side, c)  # noqa: E731

        for sx, sy in _EARS:
            ear = [(sx * 74, sy * 70 - 26), (sx * 118, sy * 70 - 4), (sx * 112, sy * 70 + 18), (sx * 70, sy * 70 - 4)]
            if sy > 0:
                ear = [(x, y + 10) for x, y in ear]
            self._poly(side, ear, fill=shade("#2b2d31"), outline=shade("#3a3d42"))
        self._poly(side, _BODY, fill=shade("#202225"), outline=shade("#34373c"), width=2)
        self._poly(side, [(x * 0.94, y * 0.94 - 3) for x, y in _BODY[:9] + _BODY[-9:]],
                   fill="", outline=shade("#2a2c30"))  # a soft top edge
        self._leds[side] = self.create_rectangle(
            *self._xy(side, -9, -100), *self._xy(side, 9, -95), fill=shade("#6b6f76"), outline=""
        )

        # face
        if side == "right":
            face = [(0, -92), (14, -90), (84, -20), (86, -6), (84, 8), (14, 78), (0, 80), (-14, 78), (-84, 8),
                    (-86, -6), (-84, -20), (-14, -90)]
            self._poly(side, face, fill=shade("#0f1012"), outline=shade("#2d3035"), width=2)
            for name, (x, y) in _FACE_POS.items():
                color = _FACE_COLORS[name]
                ring = self._oval(side, x, y, _FACE_R + 3, fill=shade("#08090a"), outline="")
                cap = self._oval(side, x, y, _FACE_R, fill=shade(color), outline=shade(_mix(color, "#ffffff", 0.25)))
                label = self.create_text(*self._xy(side, x, y + 1), text=name.upper(), fill=shade("#ffffff"),
                                         font=(_FONT, 13, "bold"))
                self._add_part(name, [ring, cap], [shade("#08090a"), shade(color)],
                               ["#ffffff", _mix(color, "#ffffff", 0.45)])
                self.addtag_withtag(f"btn:{name}", label)
        else:
            face = [(-62, -82), (62, -82), (78, -66), (78, 54), (62, 70), (-62, 70), (-78, 54), (-78, -66)]
            self._poly(side, face, fill=shade("#0f1012"), outline=shade("#2d3035"), width=2)
            inner = [(-50, -70), (50, -70), (64, -56), (64, 44), (50, 58), (-50, 58), (-64, 44), (-64, -56)]
            self._poly(side, inner, fill=shade("#17191c"), outline=shade("#26292d"))
            for name, (x, y) in _DPAD.items():
                hit = self._oval(side, x, y, 22, fill=shade("#17191c"), outline="")
                dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[name]
                px, py = -dy, dx  # perpendicular
                tip = self._xy(side, x + dx * 6, y + dy * 6)
                a = self._xy(side, x - dx * 4 + px * 9, y - dy * 4 + py * 9)
                b = self._xy(side, x - dx * 4 - px * 9, y - dy * 4 - py * 9)
                chevron = self.create_line(*a, *tip, *b, fill=shade("#d8dadd"), width=3, capstyle="round",
                                           joinstyle="round")
                self._add_part(name, [hit, chevron], [shade("#17191c"), shade("#d8dadd")], ["#2b3a2f", _GOOD])

        # paddle: the dotted grip at the bottom, with + or -
        name, glyph = _PADDLE[side]
        paddle = self._poly(side, [(-50, 84), (50, 84), (54, 100), (40, 122), (-40, 122), (-54, 100)],
                            fill=shade("#181a1d"), outline=shade("#2c2f34"))
        dots = []
        for row in range(4):
            for col in range(-8, 9):
                x, y = col * 5.2 + (2.6 if row % 2 else 0), 90 + row * 5
                if abs(x) < 44 - row * 2:
                    dots.append(self._oval(side, x, y, 1.1, fill=shade("#2f3237"), outline=""))
        sign = self.create_text(*self._xy(side, 0, 110), text=glyph, fill=shade("#e8eaed"), font=(_FONT, 16, "bold"))
        self._add_part(name, [paddle, sign], [shade("#181a1d"), shade("#e8eaed")], [_mix(_ACCENT, "#000000", 0.35), "#ffffff"])
        for dot in dots:
            self.addtag_withtag(f"btn:{name}", dot)

        if side != self._side:
            x, y = self._xy(side, 0, 146)
            self.create_text(x, y + 10, text=f"{side} pod: not used (controller_side)", fill=_MUTED, font=(_FONT, 9))

    def _draw_chips(self) -> None:
        """A key chip beside each button, joined to it by a thin line."""
        layout = {
            # name: (side, button point, chip centre)
            "y": ("right", _FACE_POS["y"], (0, -138)),
            "z": ("right", _FACE_POS["z"], (-152, -50)),
            "a": ("right", _FACE_POS["a"], (152, -50)),
            "b": ("right", _FACE_POS["b"], (152, 38)),
            "shift_up": ("right", (40, 106), (152, 112)),
            "up": ("left", _DPAD["up"], (0, -138)),
            "left": ("left", _DPAD["left"], (-152, -50)),
            "right": ("left", _DPAD["right"], (152, -50)),
            "down": ("left", _DPAD["down"], (-152, 38)),
            "shift_down": ("left", (-40, 106), (-152, 112)),
        }
        for name, (side, (bx, by), (cx, cy)) in layout.items():
            part = self._parts.get(name)
            if part is None or side != self._side:  # the unused pod's buttons never arrive
                continue
            active = True
            line_color = _PANEL_LINE if active else _mix(_PANEL_LINE, _PANEL, 0.5)
            edge = (bx + (22 if cx > bx else -22 if cx < bx else 0), by + (-22 if cy < by and cx == bx else 0))
            self.create_line(*self._xy(side, *edge), *self._xy(side, cx, cy), fill=line_color, dash=(2, 3))
            x, y = self._xy(side, cx, cy)
            part.chip = self.create_rectangle(x - 36, y - 13, x + 36, y + 13, fill=_BG if active else _PANEL,
                                              outline=line_color, width=1)
            part.chip_text = self.create_text(x, y, font=(_FONT, 10, "bold"))
            for item in (part.chip, part.chip_text):
                self.addtag_withtag(f"btn:{name}", item)
            self._refresh_chip(name)

    def _refresh_chip(self, name: str) -> None:
        part = self._parts.get(name)
        if part is None or part.chip is None:
            return
        side = "left" if name in ("up", "down", "left", "right", "shift_down") else "right"
        active = side == self._side
        if self._capturing == name:
            self.itemconfigure(part.chip, fill=_CAPTURE, outline=_CAPTURE)
            self.itemconfigure(part.chip_text, text="press a key", fill="#ffffff", font=(_FONT, 9, "bold"))
            return
        key = self._mappings[name].key
        self.itemconfigure(part.chip, fill=_BG if active else _PANEL,
                           outline=(_ACCENT if key else _PANEL_LINE) if active else _PANEL_LINE)
        color = (_TEXT if key else _MUTED) if active else _mix(_MUTED, _PANEL, 0.4)
        self.itemconfigure(part.chip_text, text=_key_text(key), fill=color, font=(_FONT, 10, "bold"))

    # state

    def refresh_all(self) -> None:
        for name in self._parts:
            self._refresh_chip(name)

    def set_capturing(self, name: str | None) -> None:
        previous, self._capturing = self._capturing, name
        for n in (previous, name):
            if n is not None:
                self._refresh_chip(n)

    def set_led(self, color: str | None) -> None:
        self.itemconfigure(self._leds[self._side], fill=color or "#6b6f76")

    def flash(self, name: str) -> None:
        part = self._parts.get(name)
        if part is None:
            return
        for item, color in zip(part.items, part.lit):
            self.itemconfigure(item, fill=color)
        if part.chip is not None and self._capturing != name:
            self.itemconfigure(part.chip, fill=_mix(_ACCENT, _BG, 0.55))
        job = self._flash_jobs.pop(name, None)
        if job is not None:
            self.after_cancel(job)
        self._flash_jobs[name] = self.after(_FLASH_MS, lambda: self._unflash(name))

    def _unflash(self, name: str) -> None:
        self._flash_jobs.pop(name, None)
        part = self._parts[name]
        for item, color in zip(part.items, part.base):
            self.itemconfigure(item, fill=color)
        self._refresh_chip(name)


class _FlatButton(tk.Label):
    """A flat, dark button (tk.Button ignores colours on some platforms)."""

    def __init__(self, parent: tk.Widget, text: str, command: Callable[[], None], accent: bool = False):
        self._bg = _ACCENT if accent else "#2a2e36"
        self._hover = _mix(self._bg, "#ffffff", 0.12)
        super().__init__(parent, text=text, bg=self._bg, fg="#ffffff", font=(_FONT, 9, "bold"),
                         padx=12, pady=5, cursor="hand2")
        self._command = command
        self._enabled = True
        self.bind("<Button-1>", lambda _e: self._enabled and self._command())
        self.bind("<Enter>", lambda _e: self._enabled and self.configure(bg=self._hover))
        self.bind("<Leave>", lambda _e: self.configure(bg=self._bg if self._enabled else "#23262c"))

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(bg=self._bg if enabled else "#23262c", fg="#ffffff" if enabled else "#5c616b",
                       cursor="hand2" if enabled else "")


class _Chip(tk.Frame):
    """A small "LABEL value" pill in the info row."""

    def __init__(self, parent: tk.Widget, label: str):
        super().__init__(parent, bg=_PANEL, highlightthickness=1, highlightbackground=_PANEL_LINE)
        tk.Label(self, text=label.upper(), bg=_PANEL, fg=_MUTED, font=(_FONT, 7, "bold")).pack(side="left", padx=(8, 4), pady=4)
        self.value = tk.Label(self, text="--", bg=_PANEL, fg=_TEXT, font=(_FONT, 9, "bold"))
        self.value.pack(side="left", padx=(0, 8))

    def set(self, text: str, color: str = _TEXT) -> None:
        self.value.configure(text=text, fg=color)


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
        controller_side: str = "right",
    ):
        self._on_toggle_listening = on_toggle_listening
        self._on_disconnect_device = on_disconnect_device
        self._on_mapping_changed = on_mapping_changed
        self._on_close = on_close
        self._mappings = {m.name: m for m in buttons.values()}
        self._side = controller_side
        self._listening = True
        self._events: queue.Queue = queue.Queue()
        self._address: str | None = None
        self._capturing: str | None = None
        self._presses = 0
        self._ever_connected = False

        self.root = tk.Tk()
        self.root.title("Zwift Click Bridge")
        self.root.configure(bg=_BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.root.bind("<KeyPress>", self._handle_key)

        # header: name, status, controls
        header = tk.Frame(self.root, bg=_BG)
        header.pack(fill="x", padx=16, pady=(14, 8))
        title = tk.Frame(header, bg=_BG)
        title.pack(side="left")
        tk.Label(title, text="ZWIFT CLICK", bg=_BG, fg=_ACCENT, font=(_FONT, 15, "bold")).pack(side="left")
        tk.Label(title, text=" BRIDGE", bg=_BG, fg=_TEXT, font=(_FONT, 15, "bold")).pack(side="left")
        tk.Label(title, text=f"  v{__version__}", bg=_BG, fg=_MUTED, font=(_FONT, 9)).pack(side="left", pady=(5, 0))
        self._toggle_button = _FlatButton(header, "Stop listening", self._handle_toggle)
        self._toggle_button.pack(side="right")
        self._disconnect_button = _FlatButton(header, "Disconnect", self._handle_disconnect)
        self._disconnect_button.pack(side="right", padx=(0, 8))

        # status + info
        status = tk.Frame(self.root, bg=_BG)
        status.pack(fill="x", padx=16, pady=(0, 10))
        self._dot = tk.Canvas(status, width=12, height=12, bg=_BG, highlightthickness=0)
        self._dot_item = self._dot.create_oval(1, 1, 11, 11, outline="")
        self._dot.pack(side="left", padx=(0, 8))
        self._status_label = tk.Label(status, bg=_BG, fg=_TEXT, font=(_FONT, 11, "bold"))
        self._status_label.pack(side="left")
        self._name_label = tk.Label(status, bg=_BG, fg=_MUTED, font=(_FONT, 9))
        self._name_label.pack(side="left", padx=8)
        self._awake_chip = _Chip(status, "keep-awake")
        self._awake_chip.pack(side="right")
        self._battery_chip = _Chip(status, "battery")
        self._battery_chip.pack(side="right", padx=6)
        self._firmware_chip = _Chip(status, "firmware")
        self._firmware_chip.pack(side="right")

        # the controller
        stage = tk.Frame(self.root, bg=_PANEL, highlightthickness=1, highlightbackground=_PANEL_LINE)
        stage.pack(padx=16)
        self._view = ControllerView(stage, buttons, controller_side, self._start_capture, self._clear_mapping)
        self._view.pack(padx=6, pady=(10, 4))
        foot = tk.Frame(stage, bg=_PANEL)
        foot.pack(fill="x", padx=12, pady=(0, 10))
        self._hint_label = tk.Label(foot, bg=_PANEL, fg=_MUTED, font=(_FONT, 9))
        self._hint_label.pack(side="left")
        self._last_label = tk.Label(foot, bg=_PANEL, fg=_TEXT, font=(_FONT, 9, "bold"))
        self._last_label.pack(side="right")
        self._show_default_hint()

        # the log, folded away; Copy puts the whole recent log on the clipboard for a bug report
        bar = tk.Frame(self.root, bg=_BG)
        bar.pack(fill="x", padx=16, pady=(10, 0))
        self._log_toggle = tk.Label(bar, text="▸ Show log", bg=_BG, fg=_MUTED, font=(_FONT, 9, "bold"),
                                    cursor="hand2")
        self._log_toggle.pack(side="left")
        self._log_toggle.bind("<Button-1>", lambda _e: self._toggle_log())
        _FlatButton(bar, "Copy log", self._copy_log).pack(side="right")
        self._copy_status = tk.Label(bar, bg=_BG, fg=_GOOD, font=(_FONT, 9))
        self._copy_status.pack(side="right", padx=8)
        self._log_panel = tk.Frame(self.root, bg=_BG)
        scrollbar = tk.Scrollbar(self._log_panel)
        scrollbar.pack(side="right", fill="y")
        self._log_text = tk.Text(self._log_panel, height=12, bg="#0d0f12", fg="#b9bec7", insertbackground="#b9bec7",
                                 font=("Consolas", 8), wrap="none", state="disabled", relief="flat",
                                 highlightthickness=0,
                                 yscrollcommand=scrollbar.set)
        self._log_text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self._log_text.yview)
        self._log_seen = 0
        tk.Frame(self.root, bg=_BG, height=12).pack(side="bottom")

        self._set_status("disconnected")
        self.root.after(_POLL_MS, self._drain_events)
        self.root.after(_LOG_POLL_MS, self._refresh_log)

    def run(self) -> None:
        self.root.mainloop()

    # -- thread-safe entry points, called from the asyncio thread --

    def on_device_status_changed(self, address: str, name: str, status: str) -> None:
        self._events.put(("status", address, name, status))

    def on_button_event(self, address: str, name: str, pressed: bool) -> None:
        if pressed:
            self._events.put(("button", address, name))

    def on_device_info(self, address: str, what: str, value: object) -> None:
        self._events.put(("info", address, what, value))

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
                self._set_status(status)  # may replace the name with a hint
            elif event[0] == "button":
                name = event[2]
                self._view.flash(name)
                self._presses += 1
                key = self._mappings[name].key if name in self._mappings else None
                arrow = f" → {_key_text(key)}" if key else ""
                self._last_label.configure(text=f"{name.replace('_', ' ')}{arrow}   ·   {self._presses} presses")
            elif event[0] == "info":
                self._show_info(event[2], event[3])
        self.root.after(_POLL_MS, self._drain_events)

    def _show_info(self, what: str, value: object) -> None:
        if what == "firmware":
            self._firmware_chip.set(str(value))
        elif what == "battery":
            level = int(value)  # type: ignore[arg-type]
            self._battery_chip.set(f"{level}%", _GOOD if level > 30 else _WARN if level > 15 else _BAD)
        elif what == "keep_awake":
            text, color = {"on": ("on", _GOOD), "off": ("off", _MUTED), "failed": ("failed", _BAD)}.get(
                str(value), (str(value), _TEXT))
            self._awake_chip.set(text, color)

    def _refresh_log(self) -> None:
        lines, self._log_seen = logview.buffer.since(self._log_seen)
        if lines:
            at_bottom = self._log_text.yview()[1] >= 0.999
            self._log_text.configure(state="normal")
            self._log_text.insert("end", "\n".join(lines) + "\n")
            extra = int(self._log_text.index("end-1c").split(".")[0]) - 1 - _LOG_VIEW_LINES
            if extra > 0:
                self._log_text.delete("1.0", f"{extra + 1}.0")
            self._log_text.configure(state="disabled")
            if at_bottom:
                self._log_text.see("end")
        self.root.after(_LOG_POLL_MS, self._refresh_log)

    def _toggle_log(self) -> None:
        if self._log_panel.winfo_ismapped():
            self._log_panel.pack_forget()
            self._log_toggle.configure(text="▸ Show log")
        else:
            self._log_panel.pack(fill="both", padx=16, pady=(6, 0))
            self._log_toggle.configure(text="▾ Hide log")
            self._log_text.see("end")

    def _copy_log(self) -> None:
        text = logview.buffer.text()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # keeps the clipboard filled on Windows after the call returns
        self._copy_status.configure(text=f"copied {text.count(chr(10)) + 1 if text else 0} lines")
        self.root.after(3000, lambda: self._copy_status.configure(text=""))

    def _set_status(self, status: str) -> None:
        color, text = _STATUS_STYLE.get(status, (_MUTED, status))
        if status == "connected":
            self._ever_connected = True
        elif status == "disconnected":
            if not self._listening:
                text = "Not listening"
            else:
                text = "Asleep" if self._ever_connected else f"Waiting for the {self._side} Click"
                self._name_label.configure(text="press a paddle to wake it")
            self._awake_chip.set("--", _MUTED)
        self._dot.itemconfigure(self._dot_item, fill=color)
        self._status_label.configure(text=text)
        self._view.set_led(color if status != "disconnected" else None)
        self._disconnect_button.set_enabled(status == "connected")

    def _show_default_hint(self) -> None:
        self._hint_label.configure(text="Click a button on the controller to remap it, right-click to unmap.")

    def _start_capture(self, name: str) -> None:
        if self._capturing == name:
            self._stop_capture()
            return
        self._capturing = name
        self._view.set_capturing(name)
        self._hint_label.configure(text=f"Press the key for '{name.replace('_', ' ')}' (Esc to cancel).")
        self.root.focus_force()

    def _stop_capture(self) -> None:
        self._capturing = None
        self._view.set_capturing(None)
        self._show_default_hint()

    def _handle_key(self, event: tk.Event) -> None:
        name = self._capturing
        if name is None:
            return
        if event.keysym == "Escape":
            self._stop_capture()
            return
        key = _key_from_event(event)
        if key is None:
            self._hint_label.configure(text=f"Can't map '{event.keysym}' -- try another key (Esc to cancel).")
            return
        self._mappings[name].key = key
        self._stop_capture()
        self._on_mapping_changed()

    def _clear_mapping(self, name: str) -> None:
        if self._capturing == name:
            self._stop_capture()
        self._mappings[name].key = None
        self._view.refresh_all()
        self._on_mapping_changed()

    def _handle_disconnect(self) -> None:
        if self._address is not None:
            self._on_disconnect_device(self._address)

    def _handle_toggle(self) -> None:
        self._listening = not self._listening
        self._toggle_button.configure(text="Stop listening" if self._listening else "Start listening")
        self._on_toggle_listening(self._listening)
        if not self._listening:
            self._status_label.configure(text="Not listening")
        elif self._dot.itemcget(self._dot_item, "fill") == _MUTED:
            self._set_status("disconnected")

    def _handle_close(self) -> None:
        self._on_close()
        self.root.destroy()
