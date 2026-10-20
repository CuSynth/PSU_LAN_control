"""Main window: supply selector, view modes, ALL ON/OFF, plots, presets, logging, sequences."""
from __future__ import annotations

import logging
import os
from typing import Optional

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QDockWidget, QDoubleSpinBox, QHBoxLayout, QInputDialog,
                               QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QToolBar, QWidget)

from dp832_worker import DataLogger, PsuWorker, profile_for
from .bridge import WorkerBridge
from .channel_panel import CH_COLORS, ChannelPanel
from .config import N_PRESETS, Config
from .dialogs import DiscoveryDialog, LimitsDialog, SequenceDialog

log = logging.getLogger("dp832.ui")

WINDOWS = [("10 s", 10), ("30 s", 30), ("1 min", 60), ("5 min", 300), ("15 min", 900), ("1 h", 3600)]
LED = {"none": "#555", "connecting": "#e6a700", "connected": "#2ecc71", "disconnected": "#e74c3c"}


class MainWindow(QMainWindow):
    error_signal = Signal(str)

    def __init__(self, cfg: Config, log_handler=None):
        super().__init__()
        self.cfg = cfg
        self.worker: Optional[PsuWorker] = None
        self.bridge: Optional[WorkerBridge] = None
        self.datalog: Optional[DataLogger] = None
        self.panels: dict[int, ChannelPanel] = {}
        self.sid: Optional[str] = None
        self.visible: list[int] = list(cfg.ui["visible"])
        self.seq_dialog: Optional[SequenceDialog] = None
        self.setWindowTitle("DP832 Control")
        self.resize(1450, 900)

        self._build_toolbars()
        self._build_central()
        self._build_log_dock(log_handler)
        self.status_lbl = QLabel("")
        self.rate_lbl = QLabel("")
        self.statusBar().addWidget(self.status_lbl, 1)
        self.statusBar().addPermanentWidget(self.rate_lbl)
        self.error_signal.connect(self._show_error)

        g = cfg.ui.get("geometry")
        if g:
            self.restoreGeometry(QByteArray.fromBase64(g.encode()))

        self.redraw_timer = QTimer(self, interval=100, timeout=self._redraw)
        self.redraw_timer.start()
        self.save_timer = QTimer(self, interval=1000, timeout=cfg.flush)
        self.save_timer.start()

        self._refresh_supply_combo()
        QTimer.singleShot(0, self._startup)

    # ------------------------------------------------------------------ UI
    def _build_toolbars(self):
        tb = QToolBar("Supply"); tb.setMovable(False)
        self.addToolBar(tb)
        self.led = QLabel("●"); self.led.setStyleSheet(f"color:{LED['none']}; font-size:16pt;")
        self.conn_lbl = QLabel("no supply")
        self.supply_box = QComboBox(); self.supply_box.setMinimumWidth(240)
        self.supply_box.setToolTip("Supplies known to this app (use Find to add more)")
        rename = QPushButton("Rename"); find = QPushButton("Find / add…")
        self.reconnect_btn = QPushButton("Reconnect")
        for w in (QLabel(" Supply: "), self.supply_box, rename, find, self.reconnect_btn, QLabel("  "), self.led, self.conn_lbl):
            tb.addWidget(w)
        rename.clicked.connect(self._rename_supply)
        find.clicked.connect(self._find_supplies)
        self.reconnect_btn.clicked.connect(lambda: self._select_supply(self.sid))
        self.supply_box.activated.connect(lambda i: self._select_supply(self.supply_box.itemData(i)))

        self.addToolBarBreak()
        t2 = QToolBar("View"); t2.setMovable(False)
        self.addToolBar(t2)
        self.mode_box = QComboBox()
        self._fill_mode_box(3)
        t2.addWidget(QLabel(" View: ")); t2.addWidget(self.mode_box)
        self.ch_btns: dict[int, QPushButton] = {}
        for c in (1, 2, 3, 4):
            b = QPushButton(f"CH{c}"); b.setCheckable(True); b.setFixedWidth(52)
            b.setToolTip("Click an inactive channel to swap it into the view")
            b.setStyleSheet(f"QPushButton:checked {{ background:{CH_COLORS[c]}; color:#14161a; font-weight:700; }}")
            b.clicked.connect(lambda _=False, c=c: self._ch_clicked(c))
            self.ch_btns[c] = b
            t2.addWidget(b)
        t2.addSeparator()
        all_on = QPushButton("ALL ON"); all_off = QPushButton("ALL OFF")
        all_on.setStyleSheet("background:#1f8a3b; color:white; font-weight:700;")
        all_off.setStyleSheet("background:#b3261e; color:white; font-weight:700;")
        t2.addWidget(all_on); t2.addWidget(all_off)
        t2.addSeparator()
        self.rate_spin = QDoubleSpinBox(); self.rate_spin.setRange(0.2, 20); self.rate_spin.setDecimals(1)
        self.rate_spin.setSuffix(" Hz"); self.rate_spin.setValue(self.cfg.ui["poll_hz"])
        self.rate_spin.setToolTip("Measurement / plot update rate (limited by the instrument's LAN speed)")
        self.win_box = QComboBox()
        for name, s in WINDOWS:
            self.win_box.addItem(name, s)
        self.win_box.setCurrentIndex(max(0, self.win_box.findData(self.cfg.ui["window_s"])))
        clear = QPushButton("Clear plots")
        t2.addWidget(QLabel(" Update: ")); t2.addWidget(self.rate_spin)
        t2.addWidget(QLabel(" Plot span: ")); t2.addWidget(self.win_box); t2.addWidget(clear)
        t2.addSeparator()
        self.rec_btn = QPushButton("● Record CSV"); self.rec_btn.setCheckable(True)
        self.rec_btn.setStyleSheet("QPushButton:checked { background:#b3261e; color:white; }")
        seq = QPushButton("Sequence…")
        self.log_btn = QPushButton("Log"); self.log_btn.setCheckable(True)
        t2.addWidget(self.rec_btn); t2.addWidget(seq); t2.addWidget(self.log_btn)

        self.mode_box.activated.connect(self._mode_changed)
        all_on.clicked.connect(self._all_on)
        all_off.clicked.connect(self._all_off)
        self.rate_spin.editingFinished.connect(self._poll_changed)
        self.win_box.activated.connect(self._window_changed)
        clear.clicked.connect(lambda: [p.clear_plots() for p in self.panels.values()])
        self.rec_btn.clicked.connect(self._toggle_record)
        seq.clicked.connect(self._open_sequence)

    def _fill_mode_box(self, n: int):
        """View choices depend on how many channels the supply has (HMP4040: up to quad)."""
        names = {1: "Single channel", 2: "Dual channel", 3: "Triple channel", 4: "Quad channel"}
        self.mode_box.blockSignals(True)
        self.mode_box.clear()
        for k in range(1, max(1, n) + 1):
            self.mode_box.addItem(names[k], k)
        self.mode_box.setCurrentIndex(max(0, min(n, self.cfg.ui["view_mode"]) - 1))
        self.mode_box.blockSignals(False)

    def _build_central(self):
        self.central = QWidget()
        self.hbox = QHBoxLayout(self.central)
        self.hbox.setSpacing(8)
        self.setCentralWidget(self.central)
        self.placeholder = QLabel("No supply connected.\nUse “Find / add…” to discover one on your network.")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("color:#8a8f98; font-size:14pt;")
        self.hbox.addWidget(self.placeholder)

    def _build_log_dock(self, handler):
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setStyleSheet("font-family:monospace; font-size:9pt;")
        dock = QDockWidget("Log", self)
        dock.setWidget(self.log_view)
        dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.log_dock = dock
        dock.setMaximumHeight(170)
        dock.visibilityChanged.connect(lambda v: self.log_btn.setChecked(v))
        self.log_btn.clicked.connect(dock.setVisible)
        dock.setVisible(bool(self.cfg.ui.get("show_log", True)))
        self.log_btn.setChecked(dock.isVisible())
        if handler:
            handler.emitter.message.connect(self._append_log)

    def _append_log(self, text: str, level: int):
        col = "#ff6b6b" if level >= logging.ERROR else "#ffb454" if level >= logging.WARNING else "#c9ced6"
        self.log_view.appendHtml(f"<span style='color:{col}'>{text.replace('<', '&lt;')}</span>")

    # ---------------------------------------------------------- supplies
    def _refresh_supply_combo(self):
        self.supply_box.blockSignals(True)
        self.supply_box.clear()
        for sid, s in self.cfg.supplies.items():
            self.supply_box.addItem(f"{s['name']}   ({s['ip']})", sid)
        i = self.supply_box.findData(self.sid)
        self.supply_box.setCurrentIndex(i)
        self.supply_box.blockSignals(False)

    def _startup(self):
        sid = self.cfg.data.get("last_supply")
        if sid in self.cfg.supplies:
            self._select_supply(sid)
        elif self.cfg.supplies:
            self._select_supply(next(iter(self.cfg.supplies)))
        else:
            self._find_supplies()

    def _find_supplies(self):
        d = self.cfg.data["discovery"]
        known = [s["ip"] for s in self.cfg.supplies.values()] + list(d.get("extra_hosts", []))
        dlg = DiscoveryDialog(self, known, d.get("ports", [5555, 5025]), d.get("timeout", 0.4))
        dlg.show()
        if dlg.exec() and dlg.selected:
            first = None
            for info in dlg.selected:
                self.cfg.upsert_supply(info)
                first = first or info.id
            self.cfg.flush()
            self._select_supply(first)

    def _rename_supply(self):
        s = self.cfg.supply(self.sid)
        if not s:
            return
        name, ok = QInputDialog.getText(self, "Rename supply", "Name for this supply:", text=s["name"])
        if ok and name.strip():
            s["name"] = name.strip()
            self.cfg.touch()
            if self.worker:
                self.worker.name = s["name"]
            if self.datalog:
                self.datalog.supply_name = s["name"]
            self._refresh_supply_combo()
            log.info("supply renamed to %r", s["name"])

    def _select_supply(self, sid: Optional[str]):
        s = self.cfg.supply(sid)
        if not s:
            return
        self._teardown_worker()
        self.sid = sid
        self.cfg.data["last_supply"] = sid
        self.cfg.touch()
        self._refresh_supply_combo()
        self._build_panels(s.get("n_channels", 3))
        self.bridge = WorkerBridge()
        self.bridge.event.connect(self._on_event)
        self.worker = PsuWorker(s["ip"], s.get("port", 5555), name=s["name"])
        self.worker.add_listener(self.bridge)
        self._set_conn("connecting", f"connecting to {s['ip']}…")
        self.worker.poll_interval = 1.0 / self.rate_spin.value()
        self.worker.poll_channels = list(self.visible)
        self.worker.start()
        log.info("selected supply %r at %s", s["name"], s["ip"])

    def _teardown_worker(self):
        if self.datalog:
            self.datalog.stop()
            self.datalog = None
            self.rec_btn.setChecked(False)
        if self.worker:
            self.worker.remove_listener(self.bridge)
            self.worker.stop(join=False)       # outputs are left exactly as they are
        self.worker = self.bridge = None

    # ------------------------------------------------------------- panels
    def _build_panels(self, n: int):
        for p in self.panels.values():
            p.setParent(None)
            p.deleteLater()
        self.panels.clear()
        self.placeholder.hide()
        prof = profile_for((self.cfg.supply(self.sid) or {}).get("model", ""))
        self._fill_mode_box(n)
        for c in range(1, n + 1):
            vmax, imax = prof.range(c)
            p = ChannelPanel(c, vmax, imax, prof)
            p.setpoint_requested.connect(self._setpoint)
            p.output_requested.connect(self._output)
            p.limits_requested.connect(self._limits)
            p.preset_action.connect(self._preset)
            p.set_window(self.win_box.currentData())
            chd = self.cfg.channel(self.sid, c)
            p.set_presets(chd["presets"])
            lim = chd["limits"]
            p.set_limits_text(lim.get("vmax", vmax), lim.get("imax", imax), None)
            last = chd["last"]                               # show last-known values until the supply answers
            for spin, k in ((p.v_spin, "v"), (p.i_spin, "i")):
                if k in last:
                    spin.blockSignals(True); spin.setValue(last[k]); spin.blockSignals(False)
            self.hbox.addWidget(p, 1)
            self.panels[c] = p
        self.visible = [c for c in self.visible if c <= n] or [1]
        self._normalise_view(self.mode_box.currentData())
        self._apply_view()

    def _normalise_view(self, mode: int):
        mode = min(mode, len(self.panels)) or 1
        vis = [c for c in self.visible if c in self.panels][-mode:]
        for c in sorted(self.panels):
            if len(vis) >= mode:
                break
            if c not in vis:
                vis.append(c)
        self.visible = sorted(vis)
        self.cfg.ui["view_mode"] = mode
        self.cfg.ui["visible"] = self.visible
        self.cfg.touch()

    def _apply_view(self):
        for c, p in self.panels.items():
            p.setVisible(c in self.visible)
        for c, b in self.ch_btns.items():
            b.setVisible(c in self.panels)
            b.setChecked(c in self.visible)
        self.mode_box.setCurrentIndex(self.cfg.ui["view_mode"] - 1)
        self._push_poll()

    def _mode_changed(self, _):
        self._normalise_view(self.mode_box.currentData())
        self._apply_view()

    def _ch_clicked(self, c: int):
        mode = self.cfg.ui["view_mode"]
        if c not in self.visible:
            self.visible = (self.visible + [c])[-mode:]
        self.cfg.ui["visible"] = self.visible = sorted(self.visible)
        self.cfg.touch()
        self._apply_view()

    def _push_poll(self):
        if self.worker:
            self.worker.set_poll(self.rate_spin.value(), self.visible)

    def _poll_changed(self):
        self.cfg.ui["poll_hz"] = self.rate_spin.value()
        self.cfg.touch()
        self._push_poll()

    def _window_changed(self, _):
        s = self.win_box.currentData()
        self.cfg.ui["window_s"] = s
        self.cfg.touch()
        for p in self.panels.values():
            p.set_window(s)

    def _redraw(self):
        for c in self.visible:
            if c in self.panels:
                self.panels[c].redraw()

    # ----------------------------------------------------------- commands
    def _need_worker(self) -> Optional[PsuWorker]:
        if self.worker and self.worker.connected:
            return self.worker
        self.status_lbl.setText("Not connected")
        return None

    def _report(self, ticket):
        ticket.add_done_callback(lambda t: t.error and self.error_signal.emit(f"{t.name}: {t.error}"))

    def _setpoint(self, ch, v, i):
        if w := self._need_worker():
            self._report(w.set_vi(ch, v, i))
            self._remember(ch, v=v, i=i)

    def _output(self, ch, on):
        if w := self._need_worker():
            self._report(w.set_output(ch, on))

    def _all_on(self):
        w = self._need_worker()
        if w and QMessageBox.question(self, "ALL ON", "Turn ON every output of this supply?") == QMessageBox.Yes:
            self._report(w.set_all_output(True))

    def _all_off(self):
        if w := self._need_worker():
            self._report(w.set_all_output(False))

    def _remember(self, ch, **kv):
        if self.sid:
            self.cfg.channel(self.sid, ch)["last"].update(kv)
            self.cfg.touch()

    def _limits(self, ch):
        w = self._need_worker()
        if not w:
            return
        hv, hi = w.ranges.get(ch, (32.0, 3.2))
        sv, si = w.limits.get(ch, (hv, hi))
        s = w.state.get(ch)
        dlg = LimitsDialog(self, ch, hv, hi, sv, si,
                           s.ovp_level if s else None, s.ovp_on if s else False,
                           s.ocp_level if s else None, s.ocp_on if s else False,
                           tripped=bool(s and (s.ovp_trip or s.ocp_trip)), profile=w.profile)
        if not dlg.exec():
            return
        if dlg.clear_trip:
            self._report(w.clear_protection(ch))
            return
        v = dlg.values()
        self._report(w.set_limits(ch, v["vmax"], v["imax"]))
        self._report(w.set_protection(ch, v["ovp"], v["ovp_on"], v["ocp"], v["ocp_on"]))
        self.cfg.channel(self.sid, ch)["limits"].update(vmax=v["vmax"], imax=v["imax"])
        self.cfg.channel(self.sid, ch)["last"].update(ovp=v["ovp"], ovp_on=v["ovp_on"],
                                                      ocp=v["ocp"], ocp_on=v["ocp_on"])
        self.cfg.touch()
        self.panels[ch].set_limits_text(v["vmax"], v["imax"], s)

    # ------------------------------------------------------------ presets
    def _preset(self, ch: int, action: str, n: int):
        chd = self.cfg.channel(self.sid, ch)
        presets = chd["presets"]
        panel = self.panels[ch]
        if action == "save":
            if presets[n] and QMessageBox.question(
                    self, "Overwrite preset", f"Overwrite preset {n + 1} of CH{ch}?") != QMessageBox.Yes:
                return
            w = self.worker
            st = panel.state
            vmax, imax = (w.limits.get(ch) if w and w.limits.get(ch) else (None, None))
            presets[n] = {
                "name": (presets[n] or {}).get("name") or f"Setup {n + 1}",
                "v": round(panel.v_spin.value(), 4), "i": round(panel.i_spin.value(), 5),
                "vmax": vmax, "imax": imax,
                "ovp": st.ovp_level if st else None, "ovp_on": st.ovp_on if st else None,
                "ocp": st.ocp_level if st else None, "ocp_on": st.ocp_on if st else None,
            }
            log.info("CH%d preset %d saved: %s", ch, n + 1, presets[n])
        elif action == "clear":
            presets[n] = None
        elif action == "rename" and presets[n]:
            name, ok = QInputDialog.getText(self, "Rename preset", "Preset name:", text=presets[n]["name"])
            if not ok:
                return
            presets[n]["name"] = name.strip() or presets[n]["name"]
        elif action == "load" and presets[n]:
            w = self._need_worker()
            if not w:
                return
            p = presets[n]
            kw = {k: p.get(k) for k in ("v", "i", "vmax", "imax", "ovp", "ovp_on", "ocp", "ocp_on")}
            self._report(w.apply_setup(ch, **kw, output=False))   # worker switches the channel OFF first
            panel._clear_dirty()
            self._remember(ch, v=p["v"], i=p["i"])
            if p.get("vmax") is not None:
                chd["limits"].update(vmax=p["vmax"], imax=p["imax"])
                panel.set_limits_text(p["vmax"], p["imax"], panel.state)
            self.status_lbl.setText(f"CH{ch}: preset {n + 1} “{p['name']}” loaded - output is OFF")
            log.info("CH%d preset %d loaded (output OFF)", ch, n + 1)
        self.cfg.touch()
        panel.set_presets(presets)

    # ---------------------------------------------------------- sequence
    def _open_sequence(self):
        if not self.panels:
            return
        if self.seq_dialog is None or not self.seq_dialog.isVisible():
            self.seq_dialog = SequenceDialog(self, sorted(self.panels), self.cfg.ui["sequence"])
        self.seq_dialog.show()
        self.seq_dialog.raise_()

    # ------------------------------------------------------------ logging
    def _toggle_record(self, on: bool):
        if not self.worker:
            self.rec_btn.setChecked(False)
            return
        if on:
            self.datalog = DataLogger(os.path.join(self.cfg.log_dir, "data"), self.worker.name)
            path = self.datalog.start()
            self.worker.add_listener(self.datalog.on_event)
            log.info("recording telemetry to %s", path)
        elif self.datalog:
            self.worker.remove_listener(self.datalog.on_event)
            self.datalog.stop()
            log.info("recording stopped (%s)", self.datalog.path)
            self.datalog = None

    # ------------------------------------------------------------- events
    def _set_conn(self, state: str, text: str):
        self.led.setStyleSheet(f"color:{LED[state]}; font-size:16pt;")
        self.conn_lbl.setText(text)

    def _on_event(self, bridge, ev):
        if bridge is not self.bridge:
            return                                    # stale event from a previous supply
        k, d = ev.kind, ev.data
        if k == "telemetry":
            for c, s in d["channels"].items():
                p = self.panels.get(c)
                if p:
                    p.update_state(s, d["ts"])
                    if s.v_set is not None:
                        self._remember_quiet(c, s)
            self.rate_lbl.setText(f"{d['rate']:.1f} Hz measured · queue {d['queue']}")
        elif k == "connection":
            self._on_connection(d)
        elif k == "sequence":
            self._on_sequence(d)
        elif k == "alarm":
            self.status_lbl.setText(f"⚠ CH{d['ch']} {d['type']} TRIPPED - output switched off by the instrument")
        elif k == "error":
            self.error_signal.emit(d["message"])

    def _remember_quiet(self, c, s):
        last = self.cfg.channel(self.sid, c)["last"]
        new = {"v": round(s.v_set, 4), "i": round(s.i_set, 5)}
        if any(last.get(k) != v for k, v in new.items()):
            last.update(new)
            self.cfg.touch()

    def _on_connection(self, d):
        st = d["state"]
        if st == "connecting":
            self._set_conn("connecting", f"connecting to {d['host']}…")
        elif st == "disconnected":
            self._set_conn("disconnected", f"disconnected - {d.get('reason', '')} (retrying)")
            for p in self.panels.values():
                p.set_sequence(None)
        elif st == "connected":
            info = d["info"]
            s = self.cfg.upsert_supply(info)             # keeps the user's name, refreshes the IP
            self._set_conn("connected", f"{info.model}  S/N {info.serial}  fw {info.firmware}   {info.host}")
            if d["n_channels"] != len(self.panels):
                self._build_panels(d["n_channels"])
            for c, (hv, hi) in d["ranges"].items():
                if c in self.panels:
                    self.panels[c].set_hw_range(hv, hi)
            w = self.worker
            for c in self.panels:                       # software limits are restored on every connect
                lim = self.cfg.channel(self.sid, c)["limits"]
                if lim:
                    w.set_limits(c, lim.get("vmax"), lim.get("imax"))
            if self.cfg.data.get("apply_last_on_connect"):
                for c in self.panels:
                    last = self.cfg.channel(self.sid, c)["last"]
                    if "v" in last:
                        self._report(w.apply_setup(c, **{k: last.get(k) for k in
                                     ("v", "i", "ovp", "ovp_on", "ocp", "ocp_on")}, output=False))
            self._push_poll()
            QTimer.singleShot(300, self._refresh_limit_labels)
            self.cfg.touch()

    def _refresh_limit_labels(self):
        w = self.worker
        if not w:
            return
        for c, p in self.panels.items():
            if c in w.limits:
                p.set_limits_text(w.limits[c][0], w.limits[c][1], w.state.get(c))

    def _on_sequence(self, d):
        p = self.panels.get(d["ch"])
        if p:
            if d["state"] == "step":
                loops = d["repeat"] or "∞"
                p.set_sequence(f"SEQ {d['step'] + 1}/{d['steps']} · {d['loop'] + 1}/{loops}")
            else:
                p.set_sequence(None)
        if self.seq_dialog:
            self.seq_dialog.on_event(d)

    def _show_error(self, msg: str):
        self.status_lbl.setText(f"⚠ {msg}")
        self.statusBar().setStyleSheet("color:#ff8a80;")
        QTimer.singleShot(8000, lambda: self.statusBar().setStyleSheet(""))

    # ------------------------------------------------------------- closing
    def closeEvent(self, e):
        self.cfg.ui["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.cfg.ui["show_log"] = self.log_dock.isVisible()
        self.cfg.touch()
        if self.seq_dialog:
            self.seq_dialog.save_cfg()
        self.cfg.save()
        self._teardown_worker()
        super().closeEvent(e)
