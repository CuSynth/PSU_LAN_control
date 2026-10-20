"""JSON configuration: supplies (IP, name), last settings, limits, presets, UI state."""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from typing import Any, Optional

log = logging.getLogger("dp832.config")

N_PRESETS = 5
CONFIG_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "last_supply": None,
    "apply_last_on_connect": False,   # re-apply last V/I/limits (output stays OFF) after connecting
    "supplies": {},
    "discovery": {"extra_hosts": [], "timeout": 0.4, "ports": [5555, 5025]},   # Rigol DP800, R&S/HAMEG HMP
    "ui": {
        "view_mode": 3,
        "visible": [1, 2, 3],
        "poll_hz": 4.0,
        "window_s": 60,
        "show_log": True,
        "geometry": None,
        "sequence": {"file": "", "channel": 1, "repeat": 1, "loop": False,
                     "output_on_start": True, "output_off_end": True},
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "supplies":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.base_dir = os.path.dirname(self.path)
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.dirty = False
        self.load()

    @property
    def log_dir(self) -> str:
        return os.path.join(self.base_dir, "logs")

    # -------------------------------------------------------------- file io
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = _merge(DEFAULTS, json.load(f))
            log.info("config loaded from %s", self.path)
        except FileNotFoundError:
            log.info("no config at %s - starting with defaults", self.path)
        except (OSError, ValueError) as e:
            bak = self.path + ".bad"
            try:
                os.replace(self.path, bak)
            except OSError:
                pass
            log.error("config unreadable (%s); moved to %s, using defaults", e, bak)

    def save(self) -> None:
        d = os.path.dirname(self.path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".cfg", suffix=".tmp", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as e:
            log.error("cannot save config: %s", e)

    def touch(self) -> None:
        self.dirty = True

    def flush(self) -> None:
        if self.dirty:
            self.save()

    # ------------------------------------------------------------- accessors
    @property
    def ui(self) -> dict:
        return self.data["ui"]

    @property
    def supplies(self) -> dict:
        return self.data["supplies"]

    def supply(self, sid: Optional[str]) -> Optional[dict]:
        return self.supplies.get(sid) if sid else None

    def upsert_supply(self, info) -> dict:
        """Create or update (IP may change with DHCP; the serial identifies the unit)."""
        sid = info.id
        s = self.supplies.get(sid)
        if s is None:
            tail = (info.serial[-4:] if info.serial else info.host)
            s = {"id": sid, "name": f"{info.model} {tail}", "channels": {}}
            self.supplies[sid] = s
        s.update(ip=info.host, port=info.port, model=info.model,
                 serial=info.serial, n_channels=info.n_channels)
        self.touch()
        return s

    def channel(self, sid: str, ch: int) -> dict:
        chans = self.supplies[sid].setdefault("channels", {})
        c = chans.setdefault(str(ch), {})
        c.setdefault("last", {})
        c.setdefault("limits", {})
        presets = c.setdefault("presets", [])
        presets.extend([None] * (N_PRESETS - len(presets)))
        del presets[N_PRESETS:]
        return c
