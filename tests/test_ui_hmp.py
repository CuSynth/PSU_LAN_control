import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from dp832_sim import Model, SimServer  # noqa: E402
from dp832_ui.app import apply_dark  # noqa: E402
from dp832_ui.bridge import setup_logging  # noqa: E402
from dp832_ui.config import Config  # noqa: E402
from dp832_ui.dialogs import LimitsDialog  # noqa: E402
from dp832_ui.main_window import MainWindow  # noqa: E402
from dp832_worker import probe, profile_for  # noqa: E402

SHOT = os.environ.get("DP832_SHOT_DIR")


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    apply_dark(a)
    return a


def pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_hmp4040_quad(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    sim = SimServer(model=Model((10.0, 10.0, 5.0, 2.0), kind="HMP4040")).start()
    cfg = Config(str(tmp_path / "cfg.json"))
    info = probe("127.0.0.1", sim.port)
    cfg.upsert_supply(info)
    cfg.data["last_supply"] = info.id
    cfg.ui["view_mode"] = 4
    cfg.ui["visible"] = [1, 2, 3, 4]
    win = MainWindow(cfg, setup_logging(cfg.log_dir))
    win.resize(1700, 950)
    win.show()
    try:
        pump(app, 1.5)
        assert win.worker.connected and win.worker.profile.family == "HMP"
        assert len(win.panels) == 4 and win.mode_box.count() == 4 and win.ch_btns[4].isVisible()
        assert win.panels[1].range_lbl.text().startswith("0–32.05 V / 0–10.01 A")
        p1, p4 = win.panels[1], win.panels[4]
        p1.v_spin.setValue(5.0); p1.i_spin.setValue(1.0); p1._apply()
        p4.v_spin.setValue(4.0); p4.i_spin.setValue(0.5); p4._apply()   # 2 ohm load -> CC at 0.5 A
        p1.out_btn.click(); p4.out_btn.click()
        pump(app, 2.5)
        m = sim.model
        assert (m.ch[1].sel and m.ch[4].sel and m.gen) and not m.ch[2].sel
        assert p1.mode_lbl.text() == "CV" and p4.mode_lbl.text() == "CC"
        assert abs(float(p4.v_lbl.text()) - 1.0) < 0.1
        # channel OFF turns only that channel off
        p1.out_btn.click(); pump(app, 1.5)
        assert not m.ch[1].sel and m.ch[4].sel and p4._out and not p1._out
        # presets round-trip with HMP protection fields
        win._preset(4, "save", 0)
        pr = cfg.channel(info.id, 4)["presets"][0]
        assert pr["v"] == 4.0 and pr["ovp"] == 32.5 and pr["ovp_on"] is True
        win._preset(4, "load", 0); pump(app, 1.0)
        assert not (m.ch[4].sel and m.gen)
        # quad / single views
        win.mode_box.setCurrentIndex(3); win._mode_changed(3)
        assert len(win.visible) == 4
        if SHOT:
            os.makedirs(SHOT, exist_ok=True)
            win.grab().save(os.path.join(SHOT, "hmp4040_quad.png"))
        win.mode_box.setCurrentIndex(1); win._mode_changed(1)
        pump(app, 0.5)
        assert len(win.visible) == 2 and sorted(win.worker.poll_channels) == win.visible
        # protection panel text
        win._limits_text = win.panels[4].lim_lbl.text()
        assert "Fuse" in win._limits_text and "OVP 32.5 V" in win._limits_text and "OVP 32.5 V ON" not in win._limits_text
        # ALL ON / ALL OFF
        win.mode_box.setCurrentIndex(3); win._mode_changed(3)
        win._all_on(); pump(app, 0.8)
        assert all(m.ch[c].sel for c in m.ch) and m.gen
        win._all_off(); pump(app, 0.8)
        assert not m.gen
    finally:
        win.close()
        sim.shutdown(); sim.server_close(); sim.kill_clients()


def test_limits_dialog_flavours(app):
    d = LimitsDialog(None, 1, 32.05, 10.01, 32.05, 10.01, 30.0, True, 50.0, True, profile=profile_for("HMP4040"))
    v = d.values()
    assert v["ocp"] == 50.0 and v["ovp"] == 30.0 and v["ocp_on"] is True and v["ovp_on"] is True
    assert d.ocp.suffix() == " ms" and d.ocp.maximum() == 250
    d2 = LimitsDialog(None, 1, 32.0, 3.2, 32.0, 3.2, 33.0, False, 3.3, False, profile=profile_for("DP832"))
    assert d2.ocp.suffix() == " A" and d2.values()["ovp_on"] is False
