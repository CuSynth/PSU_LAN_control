"""Glue between the Qt-free worker thread and the Qt main thread; logging setup."""
from __future__ import annotations

import logging
import logging.handlers
import os

from PySide6.QtCore import QObject, Signal


class WorkerBridge(QObject):
    """Use an instance as a worker listener; events are re-emitted in the GUI thread."""
    event = Signal(object, object)      # (bridge, Event)

    def __call__(self, ev) -> None:
        self.event.emit(self, ev)


class _LogEmitter(QObject):
    message = Signal(str, int)


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.emitter = _LogEmitter()
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.emitter.message.emit(self.format(record), record.levelno)
        except RuntimeError:        # emitter already destroyed at shutdown
            pass


def setup_logging(log_dir: str, level: int = logging.INFO) -> QtLogHandler:
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger("dp832")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "dp832.log"),
                                              maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(level)
    qh = QtLogHandler()
    qh.setLevel(logging.INFO)
    for h in (fh, sh, qh):
        root.addHandler(h)
    return qh
