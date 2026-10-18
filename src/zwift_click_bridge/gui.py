from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .keymap import ButtonMapping

_STATUS_STYLE = {
    "disconnected": ("#999999", "Disconnected"),
    "connecting": ("#e0b000", "Connecting..."),
    "connected": ("#2ecc71", "Connected"),
}

_FLASH_MS = 180
_INDICATOR_COLUMNS = 4


class ButtonIndicator(QFrame):
    def __init__(self, name: str, key: str | None):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._default_style = "background-color: #333333; border: 1px solid #4a4a4a; border-radius: 6px;"
        self._flash_style = "background-color: #2ecc71; border: 1px solid #2ecc71; border-radius: 6px;"
        self.setStyleSheet(self._default_style)
        self.setMinimumSize(90, 54)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)

        name_label = QLabel(name.replace("_", " "))
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setStyleSheet("color: white; font-weight: bold;")
        layout.addWidget(name_label)

        key_label = QLabel(f"-> {key}" if key else "unmapped")
        key_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        key_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        layout.addWidget(key_label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._revert)

    def flash(self) -> None:
        self.setStyleSheet(self._flash_style)
        self._timer.start(_FLASH_MS)

    def _revert(self) -> None:
        self.setStyleSheet(self._default_style)


class ControllerPanel(QWidget):
    """One of these is created per physical Click device as soon as it's first seen
    advertising; each connects/disconnects/sleeps independently of the others."""

    def __init__(
        self,
        address: str,
        name: str,
        buttons: dict[int, ButtonMapping],
        on_disconnect: Callable[[str], None],
    ):
        super().__init__()
        self.address = address
        self._on_disconnect = on_disconnect
        self.setStyleSheet("background-color: #262626; border-radius: 8px;")

        outer = QVBoxLayout(self)

        header = QHBoxLayout()
        self._dot = QLabel()
        self._dot.setFixedSize(14, 14)
        header.addWidget(self._dot)
        name_label = QLabel(name)
        name_label.setStyleSheet("font-size: 15px; font-weight: bold; color: white;")
        header.addWidget(name_label)
        self._status_label = QLabel()
        self._status_label.setStyleSheet("color: #aaaaaa;")
        header.addWidget(self._status_label)
        header.addStretch()
        self._disconnect_button = QPushButton("Disconnect")
        self._disconnect_button.setEnabled(False)
        self._disconnect_button.clicked.connect(lambda: self._on_disconnect(self.address))
        header.addWidget(self._disconnect_button)
        outer.addLayout(header)

        grid = QGridLayout()
        self._indicators: dict[str, ButtonIndicator] = {}
        for i, mapping in enumerate(buttons.values()):
            indicator = ButtonIndicator(mapping.name, mapping.key)
            self._indicators[mapping.name] = indicator
            grid.addWidget(indicator, i // _INDICATOR_COLUMNS, i % _INDICATOR_COLUMNS)
        outer.addLayout(grid)

        self.set_status("disconnected")

    def set_status(self, status: str) -> None:
        color, text = _STATUS_STYLE.get(status, ("#999999", status))
        self._dot.setStyleSheet(f"background-color: {color}; border-radius: 7px;")
        self._status_label.setText(text)
        self._disconnect_button.setEnabled(status == "connected")

    def flash_button(self, name: str) -> None:
        indicator = self._indicators.get(name)
        if indicator is not None:
            indicator.flash()


class MainWindow(QMainWindow):
    def __init__(
        self,
        buttons: dict[int, ButtonMapping],
        on_toggle_listening: Callable[[bool], None],
        on_disconnect_device: Callable[[str], None],
        on_close: Callable[[], None],
    ):
        super().__init__()
        self.setWindowTitle("Zwift Click Bridge")
        self.setStyleSheet("background-color: #1e1e1e;")
        self._buttons = buttons
        self._on_toggle_listening = on_toggle_listening
        self._on_disconnect_device = on_disconnect_device
        self._on_close = on_close
        self._listening = True
        self._panels: dict[str, ControllerPanel] = {}

        central = QWidget()
        outer = QVBoxLayout(central)

        self._empty_label = QLabel("No controllers found yet -- press a paddle to wake one.")
        self._empty_label.setStyleSheet("color: #888888;")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._empty_label)

        self._panel_container = QVBoxLayout()
        outer.addLayout(self._panel_container)
        outer.addStretch()

        self._toggle_button = QPushButton("Stop Listening")
        self._toggle_button.clicked.connect(self._handle_toggle)
        outer.addWidget(self._toggle_button)

        self.setCentralWidget(central)
        self.resize(460, 320)

    def _handle_toggle(self) -> None:
        self._listening = not self._listening
        self._toggle_button.setText("Stop Listening" if self._listening else "Start Listening")
        self._on_toggle_listening(self._listening)

    def _ensure_panel(self, address: str, name: str) -> ControllerPanel:
        panel = self._panels.get(address)
        if panel is None:
            panel = ControllerPanel(address, name, self._buttons, self._on_disconnect_device)
            self._panels[address] = panel
            self._panel_container.addWidget(panel)
            self._empty_label.setVisible(False)
        return panel

    def on_device_status_changed(self, address: str, name: str, status: str) -> None:
        panel = self._ensure_panel(address, name)
        panel.set_status(status)

    def on_button_event(self, address: str, name: str, pressed: bool) -> None:
        panel = self._panels.get(address)
        if panel is not None and pressed:
            panel.flash_button(name)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._on_close()
        super().closeEvent(event)
