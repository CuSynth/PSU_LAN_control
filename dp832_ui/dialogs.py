"""Dialogs: device discovery, V/I limits, sequence (slow signal generator)."""
from __future__ import annotations

import os

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QComboBox, QVBoxLayout, QWidget)

from dp832_worker import SequenceFileError, discover, load_sequence_file
from dp832_worker.sequence import total_duration


# --------------------------------------------------------------------------
class ScanThread(QThread):
    found = Signal(object)
    progress = Signal(int, int)
    finished_scan = Signal(int)

    def __init__(self, extra_hosts, scan_lan: bool, port: int, timeout: float, parent=None):
        super().__init__(parent)
        self.extra_hosts, self.scan_lan, self.port, self.timeout = extra_hosts, scan_lan, port, timeout
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        res = discover(self.extra_hosts, None if self.scan_lan else [], self.port, self.timeout,
                       progress=lambda d, t: self.progress.emit(d, t),
                       found=lambda i: self.found.emit(i), cancel=lambda: self._cancel)
        self.finished_scan.emit(len(res))


class DiscoveryDialog(QDialog):
    """Scan the LAN (TCP 5555 + *IDN?) or probe a typed IP; pick supplies to add."""

    def __init__(self, parent, known_hosts: list[str], port: int = 5555, timeout: float = 0.4):
        super().__init__(parent)
        self.setWindowTitle("Find power supplies")
        self.resize(560, 380)
        self.known, self.port, self.timeout = known_hosts, port, timeout
        self.devices: dict[str, object] = {}
        self.selected: list = []
        self._thread: ScanThread | None = None

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.scan_btn = QPushButton("Scan network")
        self.ip_edit = QLineEdit(); self.ip_edit.setPlaceholderText("or enter an IP address, e.g. 192.168.1.50")
        self.add_ip_btn = QPushButton("Probe IP")
        top.addWidget(self.scan_btn)
        top.addWidget(self.ip_edit, 1)
        top.addWidget(self.add_ip_btn)
        v.addLayout(top)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        v.addWidget(self.list, 1)
        self.bar = QProgressBar(); self.bar.setTextVisible(False); self.bar.setMaximumHeight(8)
        self.status = QLabel("Press “Scan network” to look for supplies on the local subnets.")
        v.addWidget(self.bar)
        v.addWidget(self.status)
        bb = QDialogButtonBox()
        self.use_btn = bb.addButton("Add / use selected", QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Close)
        v.addWidget(bb)
        self.use_btn.setEnabled(False)

        self.scan_btn.clicked.connect(lambda: self._start([], True))
        self.add_ip_btn.clicked.connect(self._probe_ip)
        self.ip_edit.returnPressed.connect(self._probe_ip)
        self.list.itemSelectionChanged.connect(lambda: self.use_btn.setEnabled(bool(self.list.selectedItems())))
        self.list.itemDoubleClicked.connect(lambda _: self._accept())
        self.use_btn.clicked.connect(self._accept)
        bb.rejected.connect(self.reject)

    def _start(self, hosts, scan_lan, timeout=None):
        if self._thread and self._thread.isRunning():
            return
        self.scan_btn.setEnabled(False); self.add_ip_btn.setEnabled(False)
        self.bar.setValue(0)
        self.status.setText("Scanning…" if scan_lan else f"Probing {hosts[0]}…")
        t = ScanThread(hosts + self.known, scan_lan, self.port, timeout or self.timeout, self)
        t.found.connect(self._found)
        t.progress.connect(lambda d, n: (self.bar.setMaximum(n), self.bar.setValue(d)))
        t.finished_scan.connect(self._done)
        self._thread = t
        t.start()

    def _probe_ip(self):
        ip = self.ip_edit.text().strip()
        if ip:
            self._start([ip], False, timeout=1.5)

    def _found(self, info):
        if info.id in self.devices:
            return
        self.devices[info.id] = info
        it = QListWidgetItem(f"{info.model}   S/N {info.serial}   {info.host}:{info.port}   fw {info.firmware}")
        it.setData(Qt.UserRole, info.id)
        self.list.addItem(it)
        it.setSelected(True)

    def _done(self, n):
        self.scan_btn.setEnabled(True); self.add_ip_btn.setEnabled(True)
        self.status.setText(f"Done - {len(self.devices)} instrument(s) found." if self.devices
                            else "No instrument answered. Check power, LAN cable and the IP settings.")

    def _accept(self):
        self.selected = [self.devices[i.data(Qt.UserRole)] for i in self.list.selectedItems()]
        if self.selected:
            self.accept()

    def done(self, r):
        if self._thread and self._thread.isRunning():
            self._thread.cancel()
            self._thread.wait(3000)
        super().done(r)


# --------------------------------------------------------------------------
class LimitsDialog(QDialog):
    """Software V/I limit (enforced by this app on every setpoint) + instrument OVP/OCP."""

    def __init__(self, parent, ch, hw_v, hw_i, vmax, imax, ovp, ovp_on, ocp, ocp_on, tripped=False):
        super().__init__(parent)
        self.setWindowTitle(f"CH{ch} limits")
        self.clear_trip = False
        f = QFormLayout(self)
        self.vmax = self._spin(hw_v, vmax, 3, " V")
        self.imax = self._spin(hw_i, imax, 4, " A")
        f.addRow(QLabel("<b>Software limits</b> (no setpoint above these is ever sent, "
                        "including preset and sequence values)"))
        f.addRow("Max voltage", self.vmax)
        f.addRow("Max current", self.imax)
        f.addRow(QLabel("<b>Instrument protection</b>"))
        self.ovp = self._spin(hw_v * 1.1, ovp if ovp is not None else hw_v, 3, " V")
        self.ovp_on = QCheckBox("OVP enabled"); self.ovp_on.setChecked(bool(ovp_on))
        self.ocp = self._spin(hw_i * 1.1, ocp if ocp is not None else hw_i, 4, " A")
        self.ocp_on = QCheckBox("OCP enabled"); self.ocp_on.setChecked(bool(ocp_on))
        f.addRow("OVP level", self._row(self.ovp, self.ovp_on))
        f.addRow("OCP level", self._row(self.ocp, self.ocp_on))
        if tripped:
            b = QPushButton("Clear trip")
            b.clicked.connect(self._clear)
            f.addRow(b)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _clear(self):
        self.clear_trip = True
        self.accept()

    @staticmethod
    def _spin(mx, val, dec, suffix):
        s = QDoubleSpinBox(); s.setDecimals(dec); s.setRange(0.0, mx); s.setSuffix(suffix)
        s.setValue(min(val, mx)); s.setSingleStep(0.1 if dec == 3 else 0.01)
        return s

    @staticmethod
    def _row(a, b):
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(a, 1); h.addWidget(b)
        return w

    def values(self) -> dict:
        return dict(vmax=self.vmax.value(), imax=self.imax.value(),
                    ovp=self.ovp.value(), ovp_on=self.ovp_on.isChecked(),
                    ocp=self.ocp.value(), ocp_on=self.ocp_on.isChecked())


# --------------------------------------------------------------------------
class SequenceDialog(QDialog):
    """Load a step file and play it on one channel (slow arbitrary signal generator)."""

    def __init__(self, main, channels: list[int], cfg: dict):
        super().__init__(main)
        self.main = main
        self.cfg = cfg
        self.steps = []
        self.setWindowTitle("Sequence player")
        self.resize(640, 560)
        v = QVBoxLayout(self)

        form = QGridLayout()
        self.file_edit = QLineEdit(cfg.get("file", ""))
        browse = QPushButton("Browse…")
        self.ch_box = QComboBox()
        for c in channels:
            self.ch_box.addItem(f"CH{c}", c)
        i = self.ch_box.findData(cfg.get("channel", 1))
        self.ch_box.setCurrentIndex(max(i, 0))
        self.repeat = QSpinBox(); self.repeat.setRange(1, 1_000_000); self.repeat.setValue(cfg.get("repeat", 1))
        self.loop = QCheckBox("loop forever"); self.loop.setChecked(cfg.get("loop", False))
        self.on_start = QCheckBox("turn output ON at start"); self.on_start.setChecked(cfg.get("output_on_start", True))
        self.off_end = QCheckBox("turn output OFF at end"); self.off_end.setChecked(cfg.get("output_off_end", True))
        form.addWidget(QLabel("File"), 0, 0); form.addWidget(self.file_edit, 0, 1); form.addWidget(browse, 0, 2)
        form.addWidget(QLabel("Channel"), 1, 0); form.addWidget(self.ch_box, 1, 1)
        form.addWidget(QLabel("Repeat"), 2, 0)
        rr = QHBoxLayout(); rr.addWidget(self.repeat); rr.addWidget(self.loop); rr.addStretch(1)
        form.addLayout(rr, 2, 1)
        form.addWidget(self.on_start, 3, 1); form.addWidget(self.off_end, 4, 1)
        v.addLayout(form)

        self.info = QLabel("Format:  duration, voltage, current   (e.g. “2.5, 3.3, 0.5”; “-” keeps the value; "
                           "durations accept ms/s/min/h)")
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        self.pv = pg.PlotWidget(background="#14161a"); self.pi = pg.PlotWidget(background="#14161a")
        for p, lab in ((self.pv, "V"), (self.pi, "A")):
            p.setLabel("left", lab); p.getAxis("left").enableAutoSIPrefix(False); p.getAxis("left").setWidth(52); p.showGrid(x=True, y=True, alpha=0.25); p.setMenuEnabled(False)
            p.setMinimumHeight(110)
        self.pi.setLabel("bottom", "time", units="s")
        self.pi.setXLink(self.pv)
        v.addWidget(self.pv, 1); v.addWidget(self.pi, 1)

        self.prog = QProgressBar(); self.prog.setFormat("%v / %m steps")
        self.state = QLabel("Idle")
        v.addWidget(self.prog); v.addWidget(self.state)
        btns = QHBoxLayout()
        self.start_btn = QPushButton("Start"); self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        btns.addStretch(1); btns.addWidget(self.start_btn); btns.addWidget(self.stop_btn)
        v.addLayout(btns)

        browse.clicked.connect(self._browse)
        self.file_edit.editingFinished.connect(self._load)
        self.loop.toggled.connect(lambda on: self.repeat.setEnabled(not on))
        self.repeat.setEnabled(not self.loop.isChecked())
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        if self.file_edit.text():
            self._load()

    # ---------------------------------------------------------------- file
    def _browse(self):
        start = os.path.dirname(self.file_edit.text()) or os.path.join(self.main.cfg.base_dir, "examples")
        path, _ = QFileDialog.getOpenFileName(self, "Sequence file", start,
                                              "Sequence files (*.csv *.txt *.seq);;All files (*)")
        if path:
            self.file_edit.setText(path)
            self._load()

    def _load(self) -> bool:
        path = self.file_edit.text().strip()
        self.steps = []
        self.pv.clear(); self.pi.clear()
        if not path:
            return False
        try:
            self.steps = load_sequence_file(path)
        except (OSError, SequenceFileError) as e:
            self.info.setText(f"<span style='color:#ff6b6b'>Cannot load: {e}</span>")
            return False
        tot = total_duration(self.steps)
        self.info.setText(f"{len(self.steps)} steps, {tot:g} s per pass")
        self._preview()
        return True

    def _preview(self):
        x = np.concatenate(([0.0], np.cumsum([s.duration for s in self.steps])))
        for plot, attr, color in ((self.pv, "v", "#f5d742"), (self.pi, "i", "#4fd1f5")):
            y, last = [], np.nan
            for s in self.steps:
                val = getattr(s, attr)
                last = last if val is None else val
                y.append(last)
            arr = np.array(y)
            plot.plot(x, arr, stepMode="center", pen=pg.mkPen(color, width=2), connect="finite")
            fin = arr[np.isfinite(arr)]
            if fin.size:
                hi = float(fin.max())
                plot.setYRange(0, hi * 1.15 if hi > 0 else 1, padding=0)

    # ------------------------------------------------------------- control
    def _start(self):
        if not self._load():
            if not self.steps:
                QMessageBox.warning(self, "Sequence", "Choose a valid sequence file first.")
            return
        w = self.main.worker
        if w is None or not w.connected:
            QMessageBox.warning(self, "Sequence", "Not connected to a supply.")
            return
        ch = self.ch_box.currentData()
        vmax, imax = w.limits.get(ch, (1e9, 1e9))
        over = sum(1 for s in self.steps if (s.v or 0) > vmax or (s.i or 0) > imax)
        if over and QMessageBox.question(
                self, "Limits",
                f"{over} step(s) exceed CH{ch}'s software limit ({vmax:g} V / {imax:g} A) and will be clamped. Continue?"
        ) != QMessageBox.Yes:
            return
        self.save_cfg()
        t = w.start_sequence(ch, self.steps, 0 if self.loop.isChecked() else self.repeat.value(),
                             self.on_start.isChecked(), self.off_end.isChecked())
        self.state.setText(f"CH{ch}: starting…")
        self.prog.setMaximum(len(self.steps)); self.prog.setValue(0)
        self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        t.add_done_callback(lambda tk: tk.error and self.main.error_signal.emit(f"Sequence: {tk.error}"))

    def _stop(self):
        w = self.main.worker
        ch = self.ch_box.currentData()
        if w:
            w.stop_sequence(ch)

    def save_cfg(self):
        self.cfg.update(file=self.file_edit.text().strip(), channel=self.ch_box.currentData(),
                        repeat=self.repeat.value(), loop=self.loop.isChecked(),
                        output_on_start=self.on_start.isChecked(), output_off_end=self.off_end.isChecked())
        self.main.cfg.touch()

    def on_event(self, d: dict):
        if d["ch"] != self.ch_box.currentData():
            return
        st = d["state"]
        if st == "step":
            loops = d["repeat"] or "∞"
            self.prog.setMaximum(d["steps"]); self.prog.setValue(d["step"] + 1)
            self.state.setText(f"CH{d['ch']} running - step {d['step'] + 1}/{d['steps']}, pass {d['loop'] + 1}/{loops}  "
                               f"(V={d['v'] if d['v'] is not None else '—'}, I={d['i'] if d['i'] is not None else '—'}, "
                               f"{d['duration']:g} s)")
        elif st in ("finished", "stopped"):
            self.state.setText(f"CH{d['ch']} {st}" + (f" ({d['reason']})" if d.get("reason") else ""))
            self.start_btn.setEnabled(True); self.stop_btn.setEnabled(False)
            if st == "finished":
                self.prog.setValue(self.prog.maximum())

    def closeEvent(self, e):
        self.save_cfg()
        super().closeEvent(e)
