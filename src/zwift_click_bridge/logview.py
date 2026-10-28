"""The recent log lines, kept in memory for the window's log panel and its Copy button."""
from __future__ import annotations

import logging
import threading
from collections import deque

MAX_LINES = 3000


class LogBuffer(logging.Handler):
    """A logging handler that keeps the last MAX_LINES formatted lines. Records arrive from
    any thread (the asyncio one mostly); the GUI asks for the lines it hasn't shown yet."""

    def __init__(self) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=MAX_LINES)
        self._total = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - a log line must never break the app
            return
        with self._lock:
            self._lines.append(line)
            self._total += 1

    def since(self, seen: int) -> tuple[list[str], int]:
        """Lines added after the first `seen` ones (as many as are still kept), and the new count."""
        with self._lock:
            new = min(self._total - seen, len(self._lines))
            return (list(self._lines)[-new:] if new > 0 else []), self._total

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines)


buffer = LogBuffer()
