#!/usr/bin/env python3
"""Tiny DP832 simulator (raw SCPI over TCP) for developing/testing without hardware.

    python tools/dp832_sim.py --port 5555 --load 10,10,5      # ohms per channel

Each channel drives a resistive load, so you see CV/CC mode changes, OVP/OCP trips, etc.
Several instances can run on different ports/serials.
"""
from __future__ import annotations

import argparse
import random
import re
import socketserver
import threading
import time

RANGES = {1: (32.0, 3.2), 2: (32.0, 3.2), 3: (5.3, 3.2)}


class Channel:
    def __init__(self, ch, load):
        self.ch, self.load = ch, load
        self.vmax, self.imax = RANGES[ch]
        self.v, self.i = 0.0, 0.0
        self.on = False
        self.ovp_on = self.ocp_on = False
        self.ovp = self.vmax + 1.0
        self.ocp = self.imax + 0.1
        self.ovp_trip = self.ocp_trip = False

    def measure(self):
        if not self.on:
            return 0.0, 0.0, "UR"
        v, i, mode = self.v, self.v / self.load, "CV"
        if i > self.i:
            i, v, mode = self.i, self.i * self.load, "CC"
        n = 1 + random.uniform(-0.002, 0.002)
        v, i = v * n, i * n
        if self.ovp_on and v > self.ovp:
            self.on, self.ovp_trip = False, True
            return 0.0, 0.0, "UR"
        if self.ocp_on and i > self.ocp:
            self.on, self.ocp_trip = False, True
            return 0.0, 0.0, "UR"
        return v, i, mode


class Model:
    def __init__(self, loads=(10.0, 10.0, 5.0), serial="DP8SIM000001"):
        self.ch = {n: Channel(n, loads[n - 1]) for n in (1, 2, 3)}
        self.serial = serial
        self.errors: list[str] = []
        self.log: list[str] = []          # every received command (handy in tests)
        self.lock = threading.Lock()


def _chan(model, arg):
    m = re.fullmatch(r"CH([123])", arg.strip().upper())
    return model.ch[int(m.group(1))] if m else None


def _num(model, ch, arg, kind):
    a = arg.strip().upper()
    lim = ch.vmax if kind == "v" else ch.imax
    if a in ("MAX", "MAXIMUM"):
        return lim
    if a in ("MIN", "MINIMUM"):
        return 0.0
    x = float(a)
    if not 0 <= x <= lim:
        raise ValueError
    return x


def handle(model: Model, line: str):
    """Return reply string or None."""
    with model.lock:
        model.log.append(line)
        line = line.strip()
        if not line:
            return None
        head, _, rest = line.partition(" ")
        is_q = head.endswith("?")
        head = head.rstrip("?")
        args = [a for a in rest.split(",")] if rest.strip() else []
        if head.upper() == "*IDN":
            return f"RIGOL TECHNOLOGIES,DP832,{model.serial},00.01.16"
        if head.upper() in ("*CLS", "*RST"):
            model.errors.clear()
            return None
        kws = [re.sub(r"[^A-Za-z0-9]", "", k).upper() for k in head.split(":") if k]
        key = tuple(re.sub(r"\d+$", "", k)[:4] for k in kws)
        num = next((int(m.group()) for k in kws if (m := re.search(r"\d+$", k))), None)
        try:
            return _dispatch(model, key, num, is_q, args)
        except (ValueError, KeyError, AttributeError):
            model.errors.append('-222,"Data out of range"')
            return None


def _dispatch(m: Model, key, num, q, args):
    if key == ("SYST", "ERRO") and q:
        return m.errors.pop(0) if m.errors else '0,"No error"'
    if key[:1] == ("SOUR",) and key[1:] in (("VOLT",), ("CURR",)):
        ch = m.ch[num]
        kind = "v" if key[1] == "VOLT" else "i"
        if q:
            if args:
                return f"{_num(m, ch, args[0], kind):.3f}"
            return f"{(ch.v if kind == 'v' else ch.i):.3f}"
        val = _num(m, ch, args[0], kind)
        if kind == "v":
            ch.v = val
        else:
            ch.i = val
        return None
    if key == ("OUTP",):
        if q:
            return "ON" if _chan(m, args[0]).on else "OFF"
        ch = _chan(m, args[0])
        ch.on = args[1].strip().upper() == "ON"
        if ch.on:
            ch.ovp_trip = ch.ocp_trip = False
        return None
    if key == ("OUTP", "MODE") and q:
        return _chan(m, args[0]).measure()[2]
    if key[:1] == ("MEAS",):
        ch = _chan(m, args[0])
        v, i, _ = ch.measure()
        if key[1:] == ("ALL",):
            return f"{v:.4f},{i:.4f},{v * i:.4f}"
        if key[1:] in (("VOLT",), ()):
            return f"{v:.4f}"
        if key[1:] == ("CURR",):
            return f"{i:.4f}"
        if key[1:] == ("POWE",):
            return f"{v * i:.4f}"
    if key[:2] in (("OUTP", "OVP"), ("OUTP", "OCP")):
        ch = _chan(m, args[0])
        ov = key[1] == "OVP"
        sub = key[2:]
        if sub == ():
            if q:
                return "ON" if (ch.ovp_on if ov else ch.ocp_on) else "OFF"
            setattr(ch, "ovp_on" if ov else "ocp_on", args[1].strip().upper() == "ON")
            return None
        if sub == ("VALU",):
            if q:
                return f"{(ch.ovp if ov else ch.ocp):.4f}"
            x = float(args[1])
            if x < 0:
                raise ValueError
            setattr(ch, "ovp" if ov else "ocp", x)
            return None
        if sub == ("QUES",) and q:
            ch.measure()
            return "YES" if (ch.ovp_trip if ov else ch.ocp_trip) else "NO"
        if sub == ("CLEA",):
            setattr(ch, "ovp_trip" if ov else "ocp_trip", False)
            return None
    m.errors.append('-113,"Undefined header"')
    return None


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        model: Model = self.server.model  # type: ignore[attr-defined]
        delay = self.server.delay          # type: ignore[attr-defined]
        self.server.clients.add(self.request)  # type: ignore[attr-defined]
        for raw in self.rfile:
            if delay:
                time.sleep(delay)
            reply = handle(model, raw.decode("ascii", "replace"))
            if reply is not None:
                self.wfile.write(reply.encode() + b"\n")


class SimServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=0, model=None, delay=0.0):
        super().__init__((host, port), _Handler)
        self.model = model or Model()
        self.delay = delay
        self.clients: set = set()

    def kill_clients(self):
        """Drop every open connection (simulates a cable pull / power cycle)."""
        for c in list(self.clients):
            try:
                c.shutdown(2)
                c.close()
            except OSError:
                pass
        self.clients.clear()

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--load", default="10,10,5", help="load resistance per channel, ohms")
    ap.add_argument("--serial", default="DP8SIM000001")
    ap.add_argument("--delay", type=float, default=0.0, help="artificial latency per command, s")
    a = ap.parse_args()
    loads = tuple(float(x) for x in a.load.split(","))
    srv = SimServer(a.host, a.port, Model(loads, a.serial), a.delay)
    print(f"DP832 simulator on {a.host}:{srv.port}  serial={a.serial}  loads={loads}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
