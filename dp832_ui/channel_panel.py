"""One channel column: readouts, setpoints, ON/OFF, presets, V and I plots."""
from __future__ import annotations

import math
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
                               QPushButton, QSizePolicy, QToolButton, QVBoxLayout)

from dp832_worker import ChannelState
from .config import N_PRESETS

CH_COLORS = {1: "#f5d742", 2: "#4fd1f5", 3: "#e879f9", 4: "#7bd88f"}
MONO = "DejaVu Sans Mono, Consolas, Menlo, monospace"


class History:
    """Growable numpy ring: columns t, v_set, v_real, i_set, i_real."""
    CAP = 120_000

    def __init__(self):
        self.buf = np.empty((5, self.CAP))
        self.n = 0

    def clear(self):
        self.n = 0

    def append(self, *row: float) -> None:
        if self.n >= self.CAP:
            half = self.CAP // 2
            self.buf[:, :half] = self.buf[:, self.n - half:self.n].copy()
            self.n = half
        self.buf[:, self.n] = row
        self.n += 1

    def since(self, t0: float) -> np.ndarray:
        k = int(np.searchsorted(self.buf[0, :self.n], t0))
        return self.buf[:, k:self.n]


class TracePlot(pg.PlotWidget):
    """Plot with a dashed 'set' line and a solid 'real' line."""

    def __init__(self, unit: str, color: str, min_span: float):
        super().__init__(background="#14161a")
        self.min_span = min_span
        pi = self.getPlotItem()
        pi.setMenuEnabled(False)
        pi.showGrid(x=True, y=True, alpha=0.25)
        pi.setLabel("left", unit)
        pi.getAxis("left").enableAutoSIPrefix(False)
        pi.getAxis("left").setWidth(52)
        pi.setMouseEnabled(x=False, y=False)
        pi.hideButtons()
        pi.addLegend(offset=(8, 4), labelTextSize="8pt")
        self.set_curve = pi.plot(pen=pg.mkPen("#d0d0d0", width=1.2, style=Qt.DashLine), name="set")
        self.real_curve = pi.plot(pen=pg.mkPen(color, width=2), name="real")
        for c in (self.set_curve, self.real_curve):
            c.setClipToView(True)
            c.setDownsampling(auto=True, method="peak")
        self.setMinimumHeight(110)

    def update_data(self, x, y_set, y_real, window: float) -> None:
        pi = self.getPlotItem()
        pi.setXRange(-window, 0, padding=0)
        self.set_curve.setData(x, y_set, connect="finite")
        self.real_curve.setData(x, y_real, connect="finite")
        if len(x):
            both = np.concatenate((y_set, y_real))
            both = both[np.isfinite(both)]
            if both.size:
                lo, hi = float(both.min()), float(both.max())
                span = max(hi - lo, self.min_span)
                mid = (hi + lo) / 2
                pi.setYRange(max(0.0, mid - span * 0.6), mid + span * 0.6, padding=0)


def _big(color: str, size: int = 30) -> QLabel:
    lb = QLabel("-")
    lb.setStyleSheet(f"color:{color}; font-family:{MONO}; font-size:{size}pt; font-weight:600;")
    lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lb


class ChannelPanel(QFrame):
    setpoint_requested = Signal(int, float, float)
    output_requested = Signal(int, bool)
    limits_requested = Signal(int)
    preset_action = Signal(int, str, int)        # channel, load|save|rename|clear, slot

    def __init__(self, ch: int, vmax: float, imax: float, profile=None):
        super().__init__()
        from dp832_worker import profile_for
        self.profile = profile or profile_for("DP832")
        self.ch, self.color = ch, CH_COLORS.get(ch, "#ffffff")
        self.vmax, self.imax = vmax, imax
        self.state: ChannelState | None = None
        self.hist = History()
        self.window = 60.0
        self._dirty = False
        self._out = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(f"ChannelPanel {{ border: 1px solid #3a3d44; border-radius: 6px; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # header -----------------------------------------------------------
        head = QHBoxLayout()
        title = QLabel(f"CH{ch}")
        title.setStyleSheet(f"color:{self.color}; font-size:18pt; font-weight:700;")
        self.range_lbl = QLabel()
        self.range_lbl.setStyleSheet("color:#8a8f98;")
        self.mode_lbl = QLabel("—")
        self.mode_lbl.setAlignment(Qt.AlignCenter)
        self.mode_lbl.setFixedWidth(52)
        self.trip_lbl = QLabel("")
        self.trip_lbl.setStyleSheet("color:white; background:#c0392b; font-weight:700; padding:1px 6px; border-radius:3px;")
        self.trip_lbl.hide()
        self.seq_lbl = QLabel("")
        self.seq_lbl.setStyleSheet("color:#14161a; background:#7bd88f; font-weight:700; padding:1px 6px; border-radius:3px;")
        self.seq_lbl.hide()
        head.addWidget(title)
        head.addWidget(self.range_lbl)
        head.addStretch(1)
        head.addWidget(self.seq_lbl)
        head.addWidget(self.trip_lbl)
        head.addWidget(self.mode_lbl)
        root.addLayout(head)

        # readouts -----------------------------------------------------------
        g = QGridLayout()
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(0)
        self.v_lbl, self.i_lbl, self.p_lbl = _big(self.color), _big(self.color), _big("#9aa0aa", 16)
        for r, (w, u) in enumerate(((self.v_lbl, "V"), (self.i_lbl, "A"), (self.p_lbl, "W"))):
            g.addWidget(w, r, 0)
            ul = QLabel(u)
            ul.setStyleSheet("color:#8a8f98; font-size:12pt;")
            g.addWidget(ul, r, 1)
        g.setColumnStretch(0, 1)
        root.addLayout(g)

        # setpoints -----------------------------------------------------------
        sp = QGridLayout()
        self.v_spin = self._spin(" V", vmax, 3, 0.1)
        self.i_spin = self._spin(" A", imax, 4, 0.01)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setToolTip("Send V and I setpoints (or press Enter in a field)")
        sp.addWidget(QLabel("Set V"), 0, 0)
        sp.addWidget(self.v_spin, 0, 1)
        sp.addWidget(QLabel("Set I"), 1, 0)
        sp.addWidget(self.i_spin, 1, 1)
        sp.addWidget(self.apply_btn, 0, 2, 2, 1)
        sp.setColumnStretch(1, 1)
        root.addLayout(sp)

        # output / limits --------------------------------------------------
        row = QHBoxLayout()
        self.out_btn = QPushButton("OUTPUT OFF")
        self.out_btn.setMinimumHeight(40)
        self.lim_btn = QPushButton("Limits…")
        row.addWidget(self.out_btn, 1)
        row.addWidget(self.lim_btn)
        root.addLayout(row)
        self.lim_lbl = QLabel("")
        self.lim_lbl.setStyleSheet("color:#8a8f98; font-size:8pt;")
        self.lim_lbl.setWordWrap(True)
        root.addWidget(self.lim_lbl)

        # presets ----------------------------------------------------------
        pg_ = QGridLayout()
        pg_.setSpacing(3)
        self.preset_btns: list[QPushButton] = []
        for n in range(N_PRESETS):
            b = QPushButton()
            b.setStyleSheet("text-align:left; padding:3px 6px;")
            b.clicked.connect(lambda _=False, n=n: self.preset_action.emit(self.ch, "load", n))
            s = QToolButton(); s.setText("Save")
            s.clicked.connect(lambda _=False, n=n: self.preset_action.emit(self.ch, "save", n))
            m = QToolButton(); m.setText("⋯"); m.setPopupMode(QToolButton.InstantPopup)
            menu = QMenu(m)
            menu.addAction("Rename…", lambda n=n: self.preset_action.emit(self.ch, "rename", n))
            menu.addAction("Clear", lambda n=n: self.preset_action.emit(self.ch, "clear", n))
            m.setMenu(menu)
            pg_.addWidget(b, n, 0)
            pg_.addWidget(s, n, 1)
            pg_.addWidget(m, n, 2)
            self.preset_btns.append(b)
        pg_.setColumnStretch(0, 1)
        root.addLayout(pg_)

        # plots --------------------------------------------------------------
        self.v_plot = TracePlot("V", self.color, 0.1)
        self.i_plot = TracePlot("A", self.color, 0.02)
        root.addWidget(self.v_plot, 1)
        root.addWidget(self.i_plot, 1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # wiring -------------------------------------------------------------
        self.apply_btn.clicked.connect(self._apply)
        self.v_spin.lineEdit().returnPressed.connect(self._apply)
        self.i_spin.lineEdit().returnPressed.connect(self._apply)
        self.v_spin.valueChanged.connect(self._mark_dirty)
        self.i_spin.valueChanged.connect(self._mark_dirty)
        self.out_btn.clicked.connect(lambda: self.output_requested.emit(self.ch, not self._out))
        self.lim_btn.clicked.connect(lambda: self.limits_requested.emit(self.ch))
        self.set_hw_range(vmax, imax)
        self._style_output()

    # ------------------------------------------------------------ helpers
    def _spin(self, suffix, mx, dec, step) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setSuffix(suffix)
        s.setDecimals(dec)
        s.setRange(0.0, mx)
        s.setSingleStep(step)
        s.setAccelerated(True)
        s.setStyleSheet(f"font-family:{MONO}; font-size:13pt;")
        s.setAlignment(Qt.AlignRight)
        return s

    def _mark_dirty(self) -> None:
        self._dirty = True
        for s in (self.v_spin, self.i_spin):
            s.setStyleSheet(s.styleSheet().split("background")[0] + "background:#5a4310;")

    def _clear_dirty(self) -> None:
        self._dirty = False
        for s in (self.v_spin, self.i_spin):
            s.setStyleSheet(s.styleSheet().split("background")[0])

    def _apply(self) -> None:
        self.setpoint_requested.emit(self.ch, self.v_spin.value(), self.i_spin.value())
        self._clear_dirty()
        self.v_spin.clearFocus()
        self.i_spin.clearFocus()

    def _style_output(self) -> None:
        if self._out:
            self.out_btn.setText("OUTPUT ON")
            self.out_btn.setStyleSheet("background:#1f8a3b; color:white; font-weight:700; font-size:12pt;")
        else:
            self.out_btn.setText("OUTPUT OFF")
            self.out_btn.setStyleSheet("background:#3a3d44; color:#d0d0d0; font-weight:700; font-size:12pt;")

    # ------------------------------------------------------------- public
    def set_hw_range(self, vmax: float, imax: float) -> None:
        self.vmax, self.imax = vmax, imax
        self.range_lbl.setText(f"0–{vmax:g} V / 0–{imax:g} A")
        self.v_spin.setMaximum(vmax)
        self.i_spin.setMaximum(imax)

    def set_limits_text(self, vmax, imax, s: ChannelState | None) -> None:
        txt = f"Software limit {vmax:g} V / {imax:g} A"
        if s and s.ovp_level is not None:
            txt += "  ·  " + self.protection_text(s.ovp_level, s.ovp_on, s.ocp_level, s.ocp_on)
        self.lim_lbl.setText(txt)
        self.v_spin.setMaximum(min(self.vmax, vmax))
        self.i_spin.setMaximum(min(self.imax, imax))

    def set_window(self, seconds: float) -> None:
        self.window = float(seconds)

    def clear_plots(self) -> None:
        self.hist.clear()

    def set_presets(self, presets: list) -> None:
        for n, (b, p) in enumerate(zip(self.preset_btns, presets)):
            if p:
                b.setText(f"{n + 1} · {p.get('name') or 'Setup'}   {p['v']:g} V  {p['i']:g} A")
                b.setToolTip(self.preset_tooltip(p))
                b.setEnabled(True)
            else:
                b.setText(f"{n + 1} · empty")
                b.setToolTip("Empty slot - use Save to store the current setup")
                b.setEnabled(False)

    def protection_text(self, ovp, ovp_on, ocp, ocp_on) -> str:
        pr = self.profile
        o = f"OVP {ovp:g} V" + (f" {'ON' if ovp_on else 'off'}" if pr.ovp_enable else "")
        if pr.ocp_kind == "fuse":
            c = f"Fuse {'ON' if ocp_on else 'off'} ({ocp:g} ms)"
        else:
            c = f"OCP {ocp:g} A {'ON' if ocp_on else 'off'}"
        return f"{o}  ·  {c}"

    def preset_tooltip(self, p: dict) -> str:
        t = f"{p['v']:g} V / {p['i']:g} A"
        if p.get("vmax") is not None:
            t += f"\nSoftware limit {p['vmax']:g} V / {p['imax']:g} A"
        if p.get("ovp") is not None:
            t += "\n" + self.protection_text(p["ovp"], p.get("ovp_on"), p["ocp"], p.get("ocp_on"))
        return t + "\nLoading turns the channel OFF first."

    def set_sequence(self, text: str | None) -> None:
        self.seq_lbl.setVisible(bool(text))
        self.seq_lbl.setText(text or "")

    def update_state(self, s: ChannelState, ts: float) -> None:
        self.state = s
        nan = math.nan
        if s.v is not None:
            self.hist.append(ts, nan if s.v_set is None else s.v_set, s.v,
                             nan if s.i_set is None else s.i_set, s.i)
            self.v_lbl.setText(f"{s.v:.3f}")
            self.i_lbl.setText(f"{s.i:.4f}")
            self.p_lbl.setText(f"{s.p:.3f}")
        if s.output is not None and s.output != self._out:
            self._out = s.output
            self._style_output()
        # mode badge
        if not self._out:
            txt, col = "OFF", "#6b7079"
        elif s.mode == "CV":
            txt, col = "CV", "#2ecc71"
        elif s.mode == "CC":
            txt, col = "CC", "#ff8c42"
        else:
            txt, col = "—", "#6b7079"
        self.mode_lbl.setText(txt)
        self.mode_lbl.setStyleSheet(f"color:{col}; border:1px solid {col}; border-radius:3px; font-weight:700;")
        ocp_name = "FUSE" if self.profile.ocp_kind == "fuse" else "OCP"
        trips = [n for n, t in (("OVP", s.ovp_trip), (ocp_name, s.ocp_trip)) if t]
        self.trip_lbl.setVisible(bool(trips))
        self.trip_lbl.setText(" ".join(trips) + " TRIP")
        # follow the instrument unless the user is editing
        if not self._dirty and not (self.v_spin.hasFocus() or self.i_spin.hasFocus()):
            for spin, val in ((self.v_spin, s.v_set), (self.i_spin, s.i_set)):
                if val is not None and abs(spin.value() - val) > 1e-6:
                    spin.blockSignals(True)
                    spin.setValue(val)
                    spin.blockSignals(False)

    def redraw(self) -> None:
        if not self.isVisible():
            return
        now = time.time()
        d = self.hist.since(now - self.window - 1)
        x = d[0] - now
        self.v_plot.update_data(x, d[1], d[2], self.window)
        self.i_plot.update_data(x, d[3], d[4], self.window)
