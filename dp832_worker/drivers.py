"""Instrument dialects.  The worker owns the queue/timing; a driver only knows SCPI.

All methods run on the worker thread and may raise ScpiError (link problem).
"""
from __future__ import annotations

import logging

from .models import ModelProfile
from .scpi import ScpiConnection, ScpiError

log = logging.getLogger("dp832.driver")


def _on(reply: str) -> bool:
    return reply.strip().upper() in ("ON", "1", "+1")


def _yes(reply: str) -> bool:
    return reply.strip().upper() in ("YES", "1", "+1", "ON")


class Driver:
    def __init__(self, conn: ScpiConnection, profile: ModelProfile):
        self.c, self.p = conn, profile

    # -- plumbing ----------------------------------------------------------
    def w(self, cmd: str) -> None:
        log.debug("> %s", cmd)
        self.c.write(cmd)

    def q(self, cmd: str) -> str:
        r = self.c.query(cmd)
        log.debug("> %s  < %s", cmd, r)
        return r

    def probe_optional(self, cmd: str) -> bool:
        """A query the instrument doesn't know never answers -> timeout.  Probe once, resync."""
        try:
            self.q(cmd)
            return True
        except ScpiError:
            self.c.open()
            self.w("*CLS")
            return False

    def drain_errors(self) -> list[str]:
        errs = []
        for _ in range(8):
            r = self.q(":SYSTem:ERRor?")
            if r.split(",", 1)[0].strip() in ("0", "+0"):
                break
            errs.append(r)
        return errs

    # -- interface ---------------------------------------------------------
    def init_session(self) -> None:
        self.w("*CLS")

    def probe_features(self) -> None: ...
    def read_ranges(self, n: int) -> dict: raise NotImplementedError
    def write_vi(self, ch, v, i) -> None: raise NotImplementedError
    def to_minimum(self, ch) -> None: raise NotImplementedError
    def set_output(self, ch, on: bool, all_ch: list) -> None: raise NotImplementedError
    def set_outputs(self, on: bool, channels: list, all_ch: list) -> None: raise NotImplementedError
    def measure(self, ch) -> tuple: raise NotImplementedError
    def begin_refresh(self) -> None: ...
    def read_basic(self, ch, s) -> None: raise NotImplementedError
    def read_protection(self, ch, s) -> None: raise NotImplementedError
    def set_protection(self, ch, ovp, ovp_on, ocp, ocp_on) -> None: raise NotImplementedError
    def clear_protection(self, ch) -> None: raise NotImplementedError


# ============================================================================
class DP800Driver(Driver):
    """Rigol DP800 series (DP832 ...): channel is an argument of each command."""

    def __init__(self, conn, profile):
        super().__init__(conn, profile)
        self.have_mode = self.have_all = True

    def probe_features(self) -> None:
        self.have_mode = self.probe_optional(":OUTPut:MODE? CH1")
        self.have_all = self.probe_optional(":MEASure:ALL? CH1")
        if not (self.have_mode and self.have_all):
            log.warning("instrument lacks MODE?/MEASure:ALL? (mode=%s all=%s); using fallbacks",
                        self.have_mode, self.have_all)

    def read_ranges(self, n: int) -> dict:
        out = {}
        for ch in range(1, n + 1):
            try:
                out[ch] = (float(self.q(f":SOURce{ch}:VOLTage? MAX")),
                           float(self.q(f":SOURce{ch}:CURRent? MAX")))
            except (ScpiError, ValueError):
                if not self.c.is_open:
                    self.c.open()
                out[ch] = self.p.range(ch)
        return out

    def write_vi(self, ch, v, i) -> None:
        if v is not None:
            self.w(f":SOURce{ch}:VOLTage {v:.3f}")
        if i is not None:
            self.w(f":SOURce{ch}:CURRent {i:.4f}")

    def to_minimum(self, ch) -> None:
        self.w(f":SOURce{ch}:VOLTage MIN")
        self.w(f":SOURce{ch}:CURRent MIN")

    def set_output(self, ch, on, all_ch) -> None:
        self.w(f":OUTPut CH{ch},{'ON' if on else 'OFF'}")

    def set_outputs(self, on, channels, all_ch) -> None:
        for ch in channels:
            self.set_output(ch, on, all_ch)

    def measure(self, ch) -> tuple:
        if self.have_all:
            v, i, p = (float(x) for x in self.q(f":MEASure:ALL? CH{ch}").split(",")[:3])
            return v, i, p
        v = float(self.q(f":MEASure:VOLTage? CH{ch}"))
        i = float(self.q(f":MEASure:CURRent? CH{ch}"))
        return v, i, v * i

    def read_basic(self, ch, s) -> None:
        s.v_set = float(self.q(f":SOURce{ch}:VOLTage?"))
        s.i_set = float(self.q(f":SOURce{ch}:CURRent?"))
        s.output = _on(self.q(f":OUTPut? CH{ch}"))
        if self.have_mode:
            s.mode = self.q(f":OUTPut:MODE? CH{ch}").strip().upper()
        s.ovp_trip = _yes(self.q(f":OUTPut:OVP:QUES? CH{ch}"))
        s.ocp_trip = _yes(self.q(f":OUTPut:OCP:QUES? CH{ch}"))

    def read_protection(self, ch, s) -> None:
        s.ovp_on = _on(self.q(f":OUTPut:OVP? CH{ch}"))
        s.ocp_on = _on(self.q(f":OUTPut:OCP? CH{ch}"))
        s.ovp_level = float(self.q(f":OUTPut:OVP:VALue? CH{ch}"))
        s.ocp_level = float(self.q(f":OUTPut:OCP:VALue? CH{ch}"))

    def set_protection(self, ch, ovp, ovp_on, ocp, ocp_on) -> None:
        if ovp is not None:
            self.w(f":OUTPut:OVP:VALue CH{ch},{float(ovp):.3f}")
        if ocp is not None:
            self.w(f":OUTPut:OCP:VALue CH{ch},{float(ocp):.4f}")
        if ovp_on is not None:
            self.w(f":OUTPut:OVP CH{ch},{'ON' if ovp_on else 'OFF'}")
        if ocp_on is not None:
            self.w(f":OUTPut:OCP CH{ch},{'ON' if ocp_on else 'OFF'}")

    def clear_protection(self, ch) -> None:
        self.w(f":OUTPut:OVP:CLEAR CH{ch}")
        self.w(f":OUTPut:OCP:CLEAR CH{ch}")


# ============================================================================
class HMPDriver(Driver):
    """R&S / HAMEG HMP4040, HMP4030 (and HMP20x0).

    Differences that matter:
      * the channel is *selected* (INST:NSEL n) and every VOLT/CURR/MEAS/OUTP:SEL/FUSE/VOLT:PROT
        command acts on the selected channel -> each channel operation starts with a select;
      * a channel only drives its output when OUTP:SEL is ON *and* the master OUTP:GEN is ON.
        Per-channel OFF = SEL OFF.  Per-channel ON = SEL ON (+ GEN ON); before GEN is switched on,
        other channels that are selected-but-idle are deselected so they don't switch on too.
      * OVP is always active; "OCP" is the electronic fuse (trip after a delay in CC mode).
    """

    ISUM_LONG = "STATus:QUEStionable:INSTrument:ISUMmary{n}:CONDition?"
    ISUM_SHORT = "STAT:QUES:ISUM{n}:COND?"

    def __init__(self, conn, profile):
        super().__init__(conn, profile)
        self.have_sel_q = self.have_gen_q = False
        self.mode_cmd: str | None = None
        self._gen: bool | None = None

    def sel(self, ch) -> None:
        self.w(f"INST:NSEL {ch}")

    def probe_features(self) -> None:
        self.sel(1)
        self.have_gen_q = self.probe_optional("OUTP:GEN?")
        self.sel(1)
        self.have_sel_q = self.probe_optional("OUTP:SEL?")
        self.mode_cmd = None
        for tmpl in (self.ISUM_LONG, self.ISUM_SHORT):
            if self.probe_optional(tmpl.format(n=1)):
                self.mode_cmd = tmpl
                break
        log.info("HMP features: OUTP:GEN?=%s OUTP:SEL?=%s mode=%s",
                 self.have_gen_q, self.have_sel_q, bool(self.mode_cmd))

    def read_ranges(self, n: int) -> dict:
        out = {}
        for ch in range(1, n + 1):
            try:
                self.sel(ch)
                out[ch] = (float(self.q("VOLT? MAX")), float(self.q("CURR? MAX")))
            except (ScpiError, ValueError):
                if not self.c.is_open:
                    self.c.open()
                out[ch] = self.p.range(ch)
        return out

    def write_vi(self, ch, v, i) -> None:
        self.sel(ch)
        if v is not None:
            self.w(f"VOLT {v:.3f}")
        if i is not None:
            self.w(f"CURR {i:.3f}")

    def to_minimum(self, ch) -> None:
        self.sel(ch)
        self.w("VOLT MIN")
        self.w("CURR MIN")

    # -- outputs -----------------------------------------------------------
    def _effective(self, ch) -> bool:
        """Is this channel really driving its output?  (channel is already selected)"""
        if self.have_sel_q and self.have_gen_q:
            return _on(self.q("OUTP:SEL?")) and _on(self.q("OUTP:GEN?"))
        return _on(self.q("OUTP?"))

    def _turn_on(self, chans, all_ch) -> None:
        for other in all_ch:
            if other not in chans:
                self.sel(other)
                if not self._effective(other):
                    self.w("OUTP:SEL OFF")      # selected-but-idle: must not follow OUTP:GEN
        for ch in chans:
            self.sel(ch)
            self.w("OUTP:SEL ON")
        self.w("OUTP:GEN ON")
        self._gen = True

    def set_output(self, ch, on, all_ch) -> None:
        if on:
            self._turn_on([ch], all_ch)
        else:
            self.sel(ch)
            self.w("OUTP:SEL OFF")

    def set_outputs(self, on, channels, all_ch) -> None:
        if on:
            self._turn_on(list(channels), all_ch)
        elif set(channels) >= set(all_ch):
            self.w("OUTP:GEN OFF")              # master switch: one command, everything off
            self._gen = False
        else:
            for ch in channels:
                self.set_output(ch, False, all_ch)

    # -- readback ----------------------------------------------------------
    def measure(self, ch) -> tuple:
        self.sel(ch)
        v = float(self.q("MEAS:VOLT?"))
        i = float(self.q("MEAS:CURR?"))
        return v, i, v * i

    def begin_refresh(self) -> None:
        self._gen = _on(self.q("OUTP:GEN?")) if self.have_gen_q else None

    def read_basic(self, ch, s) -> None:
        self.sel(ch)
        s.v_set = float(self.q("VOLT?"))
        s.i_set = float(self.q("CURR?"))
        if self.have_sel_q and self._gen is not None:
            s.output = _on(self.q("OUTP:SEL?")) and self._gen
        else:
            s.output = _on(self.q("OUTP?"))
        if self.mode_cmd:
            c = int(float(self.q(self.mode_cmd.format(n=ch))))
            s.mode = "CC" if c & 1 else "CV" if c & 2 else "UR"
        s.ovp_trip = _on(self.q("VOLT:PROT:TRIP?"))
        s.ocp_trip = _on(self.q("FUSE:TRIP?"))

    def read_protection(self, ch, s) -> None:
        self.sel(ch)
        s.ovp_on = True
        s.ovp_level = float(self.q("VOLT:PROT?"))
        s.ocp_on = _on(self.q("FUSE?"))
        s.ocp_level = float(self.q("FUSE:DEL?"))       # delay in ms

    def set_protection(self, ch, ovp, ovp_on, ocp, ocp_on) -> None:
        self.sel(ch)
        if ovp is not None:
            self.w(f"VOLT:PROT {float(ovp):.2f}")
        if ocp is not None:                            # for HMP the "OCP value" is the fuse delay, ms
            self.w(f"FUSE:DEL {int(round(float(ocp) / 10.0) * 10)}")
        if ocp_on is not None:
            self.w(f"FUSE {'ON' if ocp_on else 'OFF'}")

    def clear_protection(self, ch) -> None:
        self.sel(ch)
        self.w("VOLT:PROT:CLE")
        # a tripped fuse has no remote clear command: it re-arms when the output is switched on again


def make_driver(conn: ScpiConnection, profile: ModelProfile) -> Driver:
    return HMPDriver(conn, profile) if profile.family == "HMP" else DP800Driver(conn, profile)
