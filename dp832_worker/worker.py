"""Queue-driven worker that owns the TCP link to one DP800-series supply.

Qt-free.  Typical use::

    w = PsuWorker("192.168.1.50")
    w.add_listener(lambda ev: print(ev.kind, ev.data))
    w.start()
    t = w.set_vi(1, v=5.0, i=0.5)      # returns a Ticket immediately
    t.get(timeout=2)                    # block for the result (optional)
    w.set_output(1, True)
    w.stop()

Every public command is put on a priority queue and executed by the single
worker thread, interleaved with periodic telemetry polling and sequence
(slow signal generator) timing.  Events are delivered to listeners *from the
worker thread*; GUIs must hop threads themselves (see dp832_ui.bridge).
"""
from __future__ import annotations

import dataclasses
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from .discovery import DEFAULT_PORT, DeviceInfo, parse_idn
from .scpi import ScpiConnection, ScpiError
from .sequence import SeqStep

log = logging.getLogger("dp832.worker")

PRIO_URGENT, PRIO_NORMAL = 0, 5
FALLBACK_RANGES = {1: (32.0, 3.2), 2: (32.0, 3.2), 3: (5.3, 3.2)}


class InstrumentError(Exception):
    """The instrument accepted the link but reported an SCPI error."""


@dataclass
class Event:
    kind: str       # connection | telemetry | sequence | alarm | error
    data: Any = None


@dataclass
class ChannelState:
    ch: int
    v: Optional[float] = None
    i: Optional[float] = None
    p: Optional[float] = None
    v_set: Optional[float] = None
    i_set: Optional[float] = None
    output: Optional[bool] = None
    mode: Optional[str] = None          # CV | CC | UR
    ovp_level: Optional[float] = None
    ocp_level: Optional[float] = None
    ovp_on: Optional[bool] = None
    ocp_on: Optional[bool] = None
    ovp_trip: bool = False
    ocp_trip: bool = False


class Ticket:
    """Handle for a queued command (a tiny Future)."""

    def __init__(self, name: str):
        self.name = name
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self._ev = threading.Event()
        self._cbs: list[Callable[["Ticket"], None]] = []
        self._lock = threading.Lock()

    @property
    def done(self) -> bool:
        return self._ev.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._ev.wait(timeout)

    def get(self, timeout: Optional[float] = None) -> Any:
        if not self._ev.wait(timeout):
            raise TimeoutError(f"command {self.name} still pending")
        if self.error:
            raise self.error
        return self.result

    def add_done_callback(self, fn: Callable[["Ticket"], None]) -> None:
        with self._lock:
            if not self._ev.is_set():
                self._cbs.append(fn)
                return
        fn(self)

    def _finish(self, result: Any = None, error: Optional[BaseException] = None) -> None:
        with self._lock:
            self.result, self.error = result, error
            self._ev.set()
            cbs, self._cbs = self._cbs, []
        for fn in cbs:
            try:
                fn(self)
            except Exception:
                log.exception("ticket callback failed")


@dataclass
class _Command:
    name: str
    args: dict
    ticket: Ticket


class _SeqRunner:
    def __init__(self, ch, steps, repeat, on_start, off_end):
        self.ch, self.steps, self.repeat = ch, steps, repeat
        self.on_start, self.off_end = on_start, off_end
        self.idx = -1
        self.loop = 0
        self.deadline = 0.0          # monotonic time at which the current step ends


class PsuWorker:
    SLOW_INTERVAL = 1.0
    PROT_INTERVAL = 5.0

    def __init__(self, host: str, port: int = DEFAULT_PORT, name: str = "",
                 timeout: float = 2.0, reconnect_s: float = 2.0):
        self.host, self.port = host, port
        self.name = name or host
        self.reconnect_s = reconnect_s
        self._conn = ScpiConnection(host, port, timeout)
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._count = itertools.count()
        self._listeners: list[Callable[[Event], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self.connected = False
        self.info: Optional[DeviceInfo] = None
        self.n_channels = 3
        self.ranges: dict[int, tuple[float, float]] = {}
        self.limits: dict[int, list[float]] = {}      # software limits [vmax, imax]
        self.state: dict[int, ChannelState] = {}
        self.poll_interval = 0.25
        self.poll_channels: list[int] = [1, 2, 3]
        self.measured_rate = 0.0
        self._seq: dict[int, _SeqRunner] = {}
        self._have_mode = self._have_all = True
        self._next_poll = self._next_slow = self._next_prot = 0.0
        self._last_poll_t = 0.0
        self._last_reason = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"psu-{self.host}", daemon=True)
        self._thread.start()

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        if join and self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._conn.close()
        self.connected = False
        self._fail_pending("worker stopped")

    def add_listener(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[Event], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _emit(self, kind: str, data: Any = None) -> None:
        ev = Event(kind, data)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                log.exception("listener failed")

    # ------------------------------------------------------------ public API
    def submit(self, name: str, priority: int = PRIO_NORMAL, **args) -> Ticket:
        if not hasattr(self, "_cmd_" + name):
            raise ValueError(f"unknown command {name!r}")
        t = Ticket(name)
        self._q.put((priority, next(self._count), _Command(name, args, t)))
        return t

    @property
    def queue_depth(self) -> int:
        return self._q.qsize()

    def set_voltage(self, ch, v): return self.submit("set_vi", ch=ch, v=v)
    def set_current(self, ch, i): return self.submit("set_vi", ch=ch, i=i)
    def set_vi(self, ch, v=None, i=None): return self.submit("set_vi", ch=ch, v=v, i=i)

    def set_output(self, ch, on: bool):
        return self.submit("set_output", PRIO_URGENT if not on else PRIO_NORMAL, ch=ch, on=on)

    def set_all_output(self, on: bool, channels: Optional[Iterable[int]] = None):
        return self.submit("set_all_output", PRIO_URGENT if not on else PRIO_NORMAL,
                           on=on, channels=list(channels) if channels else None)

    def apply_setup(self, ch, **setup):
        """Load a stored setup: output OFF first, then limits, then V/I."""
        return self.submit("apply_setup", ch=ch, **setup)

    def set_limits(self, ch, vmax=None, imax=None): return self.submit("set_limits", ch=ch, vmax=vmax, imax=imax)

    def set_protection(self, ch, ovp=None, ovp_on=None, ocp=None, ocp_on=None):
        return self.submit("set_protection", ch=ch, ovp=ovp, ovp_on=ovp_on, ocp=ocp, ocp_on=ocp_on)

    def clear_protection(self, ch): return self.submit("clear_protection", ch=ch)
    def set_poll(self, rate_hz: float, channels=None): return self.submit("set_poll", rate_hz=rate_hz, channels=channels)
    def refresh(self): return self.submit("refresh")
    def scpi(self, text: str): return self.submit("scpi", text=text)

    def start_sequence(self, ch, steps: list[SeqStep], repeat=1, output_on_start=True, output_off_end=True):
        return self.submit("start_sequence", ch=ch, steps=steps, repeat=repeat,
                           output_on_start=output_on_start, output_off_end=output_off_end)

    def stop_sequence(self, ch=None, output_off=False):
        return self.submit("stop_sequence", PRIO_URGENT, ch=ch, output_off=output_off)

    def sequence_running(self, ch) -> bool:
        return ch in self._seq

    # --------------------------------------------------------------- thread
    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.connected:
                try:
                    self._try_connect()
                except ScpiError as e:
                    self._comm_failure(str(e))
                if not self.connected:
                    self._fail_pending("not connected")
                    self._stop.wait(self.reconnect_s)
                    continue
            try:
                self._loop_once()
            except ScpiError as e:
                if self._stop.is_set():
                    break               # shutting down: closed socket is expected
                self._comm_failure(str(e))
            except Exception:
                log.exception("unexpected worker error")
                self._stop.wait(0.2)

    def _loop_once(self) -> None:
        now = time.monotonic()
        due = [self._next_poll, self._next_slow] + [r.deadline for r in self._seq.values()]
        timeout = min(max(0.0, min(due) - now), 0.5)
        try:
            _, _, cmd = self._q.get(timeout=timeout)
        except queue.Empty:
            cmd = None
        if cmd:
            self._execute(cmd)
        now = time.monotonic()
        if self._seq:
            self._tick_sequences(now)
        if now >= self._next_slow:
            self._slow_refresh()
            self._next_slow = time.monotonic() + self.SLOW_INTERVAL
        if now >= self._next_poll:
            self._poll()
            self._next_poll = max(now + self.poll_interval, time.monotonic())

    # ------------------------------------------------------------ connection
    def _try_connect(self) -> None:
        if self._last_reason is None:
            self._emit("connection", {"state": "connecting", "host": self.host})
        try:
            self._conn.open()
            self._conn.write("*CLS")
            idn = self._conn.query("*IDN?")
            self.info = parse_idn(self.host, self.port, idn)
            self.n_channels = self.info.n_channels
            if "DP8" not in self.info.model.upper():
                log.warning("unexpected instrument %r - SCPI subset may not match", idn)
            self._probe_features()
            self._read_ranges()
        except ScpiError as e:
            self._conn.close()
            if str(e) != self._last_reason:
                log.warning("connect to %s failed: %s", self.host, e)
                self._emit("connection", {"state": "disconnected", "reason": str(e)})
            self._last_reason = str(e)
            return
        self.state = {ch: ChannelState(ch) for ch in range(1, self.n_channels + 1)}
        self.poll_channels = [c for c in self.poll_channels if c <= self.n_channels] or [1]
        self.connected = True
        self._last_reason = None
        self._next_poll = self._next_slow = self._next_prot = 0.0
        log.info("connected to %s (%s)", self.host, self.info.idn)
        self._slow_refresh(full=True)
        self._emit("connection", {
            "state": "connected", "info": self.info, "n_channels": self.n_channels,
            "ranges": dict(self.ranges), "limits": {c: tuple(v) for c, v in self.limits.items()},
        })

    def _probe_optional(self, cmd: str) -> bool:
        """A query that an instrument doesn't know never answers -> timeout.  Probe once."""
        try:
            self._conn.query(cmd)
            return True
        except ScpiError:
            self._conn.open()           # resync the stream
            self._conn.write("*CLS")
            return False

    def _probe_features(self) -> None:
        self._have_mode = self._probe_optional(":OUTPut:MODE? CH1")
        self._have_all = self._probe_optional(":MEASure:ALL? CH1")
        if not (self._have_mode and self._have_all):
            log.warning("instrument lacks MODE?/MEASure:ALL? (mode=%s all=%s); using fallbacks",
                        self._have_mode, self._have_all)

    def _read_ranges(self) -> None:
        for ch in range(1, self.n_channels + 1):
            fb = FALLBACK_RANGES.get(ch, (32.0, 3.2))
            try:
                vmax = float(self._conn.query(f":SOURce{ch}:VOLTage? MAX"))
                imax = float(self._conn.query(f":SOURce{ch}:CURRent? MAX"))
            except (ScpiError, ValueError):
                if not self._conn.is_open:
                    self._conn.open()
                vmax, imax = fb
            self.ranges[ch] = (vmax, imax)
            old = self.limits.get(ch)
            self.limits[ch] = [min(old[0], vmax), min(old[1], imax)] if old else [vmax, imax]

    def _comm_failure(self, reason: str) -> None:
        log.error("link lost: %s", reason)
        self._conn.close()
        self.connected = False
        self._last_reason = reason
        self._stop_sequences(f"connection lost: {reason}")
        self._emit("connection", {"state": "disconnected", "reason": reason})
        self._fail_pending(reason)

    def _fail_pending(self, why: str) -> None:
        while True:
            try:
                _, _, cmd = self._q.get_nowait()
            except queue.Empty:
                return
            cmd.ticket._finish(error=ScpiError(why))

    # ---------------------------------------------------------------- scpi
    def _w(self, cmd: str) -> None:
        log.debug("> %s", cmd)
        self._conn.write(cmd)

    def _q_(self, cmd: str) -> str:
        r = self._conn.query(cmd)
        log.debug("> %s  < %s", cmd, r)
        return r

    def _drain_errors(self) -> list[str]:
        errs = []
        for _ in range(8):
            r = self._q_(":SYSTem:ERRor?")
            if r.split(",", 1)[0].strip() in ("0", "+0"):
                break
            errs.append(r)
        return errs

    def _check(self, context: str) -> None:
        errs = self._drain_errors()
        if errs:
            msg = f"{context}: instrument error {'; '.join(errs)}"
            log.warning(msg)
            raise InstrumentError(msg)

    # ------------------------------------------------------------- commands
    def _execute(self, cmd: _Command) -> None:
        try:
            res = getattr(self, "_cmd_" + cmd.name)(**cmd.args)
        except ScpiError as e:
            cmd.ticket._finish(error=e)
            raise
        except Exception as e:
            log.warning("command %s failed: %s", cmd.name, e)
            cmd.ticket._finish(error=e)
            self._emit("error", {"command": cmd.name, "message": str(e)})
        else:
            cmd.ticket._finish(result=res)
        self._next_slow = 0.0    # re-read the instrument soon after any command
        if self.state:
            self._emit_telemetry()

    def _chan(self, ch: int) -> int:
        if not 1 <= int(ch) <= self.n_channels:
            raise ValueError(f"channel {ch} out of range 1..{self.n_channels}")
        return int(ch)

    def _clamp(self, ch, v, i):
        vmax, imax = self.limits[ch]
        out = []
        for val, lim, what in ((v, vmax, "V"), (i, imax, "A")):
            if val is not None and val > lim:
                log.warning("CH%d: %s setpoint %.3f clamped to limit %.3f", ch, what, val, lim)
                val = lim
            out.append(None if val is None else max(0.0, float(val)))
        return out

    def _write_vi(self, ch, v, i) -> tuple:
        v, i = self._clamp(ch, v, i)
        s = self.state[ch]
        if v is not None:
            self._w(f":SOURce{ch}:VOLTage {v:.3f}")
            s.v_set = v
        if i is not None:
            self._w(f":SOURce{ch}:CURRent {i:.4f}")
            s.i_set = i
        return v, i

    def _cmd_set_vi(self, ch, v=None, i=None):
        ch = self._chan(ch)
        res = self._write_vi(ch, v, i)
        self._check(f"CH{ch} set V/I")
        return res

    def _set_out(self, ch, on):
        self._w(f":OUTPut CH{ch},{'ON' if on else 'OFF'}")
        self.state[ch].output = bool(on)
        log.info("CH%d output %s", ch, "ON" if on else "OFF")

    def _cmd_set_output(self, ch, on):
        ch = self._chan(ch)
        self._set_out(ch, on)
        self._check(f"CH{ch} output")

    def _cmd_set_all_output(self, on, channels=None):
        for ch in (channels or range(1, self.n_channels + 1)):
            self._set_out(self._chan(ch), on)
        self._check("all outputs")

    def _cmd_set_limits(self, ch, vmax=None, imax=None):
        ch = self._chan(ch)
        hv, hi = self.ranges[ch]
        if vmax is not None:
            self.limits[ch][0] = min(float(vmax), hv)
        if imax is not None:
            self.limits[ch][1] = min(float(imax), hi)
        log.info("CH%d software limits: %.3f V / %.3f A", ch, *self.limits[ch])
        return tuple(self.limits[ch])

    def _cmd_set_protection(self, ch, ovp=None, ovp_on=None, ocp=None, ocp_on=None):
        ch = self._chan(ch)
        if ovp is not None:
            self._w(f":OUTPut:OVP:VALue CH{ch},{float(ovp):.3f}")
        if ocp is not None:
            self._w(f":OUTPut:OCP:VALue CH{ch},{float(ocp):.4f}")
        if ovp_on is not None:
            self._w(f":OUTPut:OVP CH{ch},{'ON' if ovp_on else 'OFF'}")
        if ocp_on is not None:
            self._w(f":OUTPut:OCP CH{ch},{'ON' if ocp_on else 'OFF'}")
        self._next_prot = 0.0
        self._check(f"CH{ch} protection")

    def _cmd_clear_protection(self, ch):
        ch = self._chan(ch)
        self._w(f":OUTPut:OVP:CLEAR CH{ch}")
        self._w(f":OUTPut:OCP:CLEAR CH{ch}")
        self._check(f"CH{ch} clear protection")

    def _cmd_apply_setup(self, ch, v=None, i=None, vmax=None, imax=None,
                         ovp=None, ovp_on=None, ocp=None, ocp_on=None, output=False):
        ch = self._chan(ch)
        self._stop_sequences("setup loaded", only=ch)
        self._set_out(ch, False)                     # always off first
        if vmax is not None or imax is not None:
            self._cmd_set_limits(ch, vmax, imax)
        # drop to minimum so a new OVP/OCP below the old setpoint is accepted
        self._w(f":SOURce{ch}:VOLTage MIN")
        self._w(f":SOURce{ch}:CURRent MIN")
        if any(x is not None for x in (ovp, ovp_on, ocp, ocp_on)):
            self._cmd_set_protection(ch, ovp, ovp_on, ocp, ocp_on)
        self._write_vi(ch, v, i)
        if output:
            self._set_out(ch, True)
        self._check(f"CH{ch} apply setup")
        log.info("CH%d setup applied (V=%s I=%s)", ch, v, i)

    def _cmd_set_poll(self, rate_hz, channels=None):
        self.poll_interval = 1.0 / min(max(float(rate_hz), 0.2), 20.0)
        if channels is not None:
            self.poll_channels = sorted({c for c in channels if 1 <= c <= self.n_channels})
        self._next_poll = 0.0

    def _cmd_refresh(self):
        self._slow_refresh(full=True)

    def _cmd_scpi(self, text):
        text = text.strip()
        if "?" in text:
            return self._q_(text)
        self._w(text)
        return None

    # ------------------------------------------------------------ sequences
    def _cmd_start_sequence(self, ch, steps, repeat=1, output_on_start=True, output_off_end=True):
        ch = self._chan(ch)
        if not steps:
            raise ValueError("empty sequence")
        vmax, imax = self.limits[ch]
        n = sum(1 for s in steps if (s.v or 0) > vmax or (s.i or 0) > imax)
        if n:
            log.warning("CH%d sequence: %d step(s) exceed the software limits and will be clamped", ch, n)
        self._stop_sequences("replaced", only=ch)
        r = _SeqRunner(ch, list(steps), int(repeat), output_on_start, output_off_end)
        r.deadline = 0.0
        self._seq[ch] = r
        log.info("CH%d sequence started: %d steps, repeat=%s", ch, len(steps), repeat or "inf")
        self._emit("sequence", {"ch": ch, "state": "started", "steps": len(steps), "repeat": repeat})
        self._tick_sequences(time.monotonic())

    def _cmd_stop_sequence(self, ch=None, output_off=False):
        chans = [ch] if ch is not None else list(self._seq)
        self._stop_sequences("stopped by user", only=ch)
        if output_off:
            for c in chans:
                self._set_out(self._chan(c), False)
            self._check("sequence stop")

    def _stop_sequences(self, reason, only=None) -> None:
        for ch in list(self._seq):
            if only is None or ch == only:
                r = self._seq.pop(ch)
                log.info("CH%d sequence stopped (%s)", ch, reason)
                self._emit("sequence", {"ch": ch, "state": "stopped", "reason": reason,
                                        "step": r.idx, "steps": len(r.steps)})

    def _tick_sequences(self, now: float) -> None:
        for ch, r in list(self._seq.items()):
            if now < r.deadline:
                continue
            first = r.idx < 0
            ended_at = now if first else r.deadline      # absolute schedule -> no drift
            if first:
                r.idx = 0
            else:
                r.idx += 1
                if r.idx >= len(r.steps):
                    r.loop += 1
                    if r.repeat and r.loop >= r.repeat:
                        self._seq.pop(ch)
                        if r.off_end:
                            self._set_out(ch, False)
                        log.info("CH%d sequence finished", ch)
                        self._emit("sequence", {"ch": ch, "state": "finished",
                                                "steps": len(r.steps), "loop": r.loop})
                        continue
                    r.idx = 0
            st = r.steps[r.idx]
            v, i = self._write_vi(ch, st.v, st.i)
            if first and r.on_start:
                self._set_out(ch, True)
            r.deadline = ended_at + st.duration
            self._emit("sequence", {"ch": ch, "state": "step", "step": r.idx, "steps": len(r.steps),
                                    "loop": r.loop, "repeat": r.repeat, "v": v, "i": i,
                                    "duration": st.duration})
        if self.state:
            self._emit_telemetry()

    # ------------------------------------------------------------ telemetry
    def _poll(self) -> None:
        if not self.poll_channels:
            return
        for ch in self.poll_channels:
            s = self.state[ch]
            if self._have_all:
                parts = self._q_(f":MEASure:ALL? CH{ch}").split(",")
                s.v, s.i, s.p = (float(x) for x in parts[:3])
            else:
                s.v = float(self._q_(f":MEASure:VOLTage? CH{ch}"))
                s.i = float(self._q_(f":MEASure:CURRent? CH{ch}"))
                s.p = s.v * s.i
        now = time.monotonic()
        if self._last_poll_t:
            inst = 1.0 / max(now - self._last_poll_t, 1e-3)
            self.measured_rate = inst if not self.measured_rate else 0.8 * self.measured_rate + 0.2 * inst
        self._last_poll_t = now
        self._emit_telemetry()

    def _slow_refresh(self, full: bool = False) -> None:
        now = time.monotonic()
        do_prot = full or now >= self._next_prot
        for ch in (range(1, self.n_channels + 1) if full else self.poll_channels):
            s = self.state[ch]
            s.v_set = float(self._q_(f":SOURce{ch}:VOLTage?"))
            s.i_set = float(self._q_(f":SOURce{ch}:CURRent?"))
            s.output = self._q_(f":OUTPut? CH{ch}").upper().startswith("ON")
            if self._have_mode:
                s.mode = self._q_(f":OUTPut:MODE? CH{ch}").strip().upper()
            ovp_t = self._q_(f":OUTPut:OVP:QUES? CH{ch}").upper().startswith("YES")
            ocp_t = self._q_(f":OUTPut:OCP:QUES? CH{ch}").upper().startswith("YES")
            for flag, new, label in (("ovp_trip", ovp_t, "OVP"), ("ocp_trip", ocp_t, "OCP")):
                if new and not getattr(s, flag):
                    log.warning("CH%d %s TRIPPED", ch, label)
                    self._emit("alarm", {"ch": ch, "type": label})
                setattr(s, flag, new)
            if do_prot:
                s.ovp_on = self._q_(f":OUTPut:OVP? CH{ch}").upper().startswith("ON")
                s.ocp_on = self._q_(f":OUTPut:OCP? CH{ch}").upper().startswith("ON")
                s.ovp_level = float(self._q_(f":OUTPut:OVP:VALue? CH{ch}"))
                s.ocp_level = float(self._q_(f":OUTPut:OCP:VALue? CH{ch}"))
        if do_prot:
            self._next_prot = time.monotonic() + self.PROT_INTERVAL
        self._emit_telemetry()

    def _emit_telemetry(self) -> None:
        chans = {ch: dataclasses.replace(s) for ch, s in self.state.items()
                 if ch in self.poll_channels}
        self._emit("telemetry", {"ts": time.time(), "supply": self.name, "channels": chans,
                                 "rate": self.measured_rate, "queue": self._q.qsize()})
