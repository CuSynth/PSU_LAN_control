"""CSV recording of telemetry events (attach with worker.add_listener)."""
from __future__ import annotations

import csv
import datetime as dt
import os
import re
import threading

from .worker import Event


class DataLogger:
    HEADER = ["timestamp_iso", "t_unix", "supply", "channel", "v_set", "i_set",
              "v_meas", "i_meas", "p_meas", "output", "mode"]

    def __init__(self, directory: str, supply_name: str):
        self.directory = directory
        self.supply_name = supply_name
        self.path: str | None = None
        self._f = None
        self._w = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._f is not None

    def start(self) -> str:
        with self._lock:
            os.makedirs(self.directory, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.supply_name) or "supply"
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.path = os.path.join(self.directory, f"data_{safe}_{stamp}.csv")
            self._f = open(self.path, "w", newline="", encoding="utf-8")
            self._w = csv.writer(self._f)
            self._w.writerow(self.HEADER)
            return self.path

    def stop(self) -> None:
        with self._lock:
            if self._f:
                self._f.close()
            self._f = self._w = None

    def on_event(self, ev: Event) -> None:
        if ev.kind != "telemetry" or not self.active:
            return
        t = ev.data["ts"]
        iso = dt.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")
        with self._lock:
            if not self._w:
                return
            for ch, s in sorted(ev.data["channels"].items()):
                self._w.writerow([iso, f"{t:.3f}", self.supply_name, ch,
                                  _f(s.v_set), _f(s.i_set), _f(s.v), _f(s.i), _f(s.p),
                                  "ON" if s.output else "OFF", s.mode or ""])
            self._f.flush()


def _f(x):
    return "" if x is None else f"{x:.5g}"
