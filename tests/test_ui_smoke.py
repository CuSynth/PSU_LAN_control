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
from dp832_ui.main_window import MainWindow  # noqa: E402
from dp832_worker import probe  # noqa: E402

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


def shot(win, name):
    if SHOT:
        os.makedirs(SHOT, exist_ok=True)
        win.grab().save(os.path.join(SHOT, name))


@pytest.fixture()
def env(app, tmp_path):
    sim = SimServer(model=Model((10.0, 10.0, 5.0))).start()
    cfg = Config(str(tmp_path / "cfg.json"))
    cfg.upsert_supply(probe("127.0.0.1", sim.port))
    cfg.data["last_supply"] = "DP8SIM000001"
    cfg.save()
    h = setup_logging(cfg.log_dir)
    win = MainWindow(cfg, h)
    win.resize(1500, 950)
    win.show()
    yield sim, cfg, win
    win.close()
    sim.shutdown(); sim.server_close(); sim.kill_clients()


def test_full_flow(app, env, monkeypatch, tmp_path):
    sim, cfg, win = env
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    pump(app, 1.5)
    assert win.worker.connected and len(win.panels) == 3
    assert "connected" in win.conn_lbl.text() or "DP832" in win.conn_lbl.text()

    # setpoints + output on CH1 / CH2
    p1, p2 = win.panels[1], win.panels[2]
    p1.v_spin.setValue(5.0); p1.i_spin.setValue(1.0); p1._apply()
    p2.v_spin.setValue(3.0); p2.i_spin.setValue(0.1); p2._apply()
    p1.out_btn.click(); p2.out_btn.click()
    pump(app, 2.5)
    assert sim.model.ch[1].on and sim.model.ch[2].on
    assert p1.hist.n > 5
    assert p1.mode_lbl.text() == "CV" and p2.mode_lbl.text() == "CC"
    assert abs(float(p1.v_lbl.text()) - 5.0) < 0.1
    shot(win, "triple.png")

    # presets: save, change, load -> channel must switch OFF
    win._preset(1, "save", 0)
    win._preset(1, "rename", 0) if False else None
    p1.v_spin.setValue(9.0); p1._apply()
    pump(app, 0.6)
    win._preset(1, "load", 0)
    pump(app, 1.0)
    assert not sim.model.ch[1].on and sim.model.ch[1].v == 5.0
    assert "empty" not in p1.preset_btns[0].text() and "empty" in p1.preset_btns[1].text()

    # views
    win.mode_box.setCurrentIndex(1); win._mode_changed(1)
    assert len(win.visible) == 2 and sum(p.isVisible() for p in win.panels.values()) == 2
    win._ch_clicked(3)
    assert 3 in win.visible and len(win.visible) == 2
    pump(app, 1.0)
    assert set(win.worker.poll_channels) == set(win.visible)
    shot(win, "dual.png")
    win.mode_box.setCurrentIndex(0); win._mode_changed(0)
    assert len(win.visible) == 1
    pump(app, 0.8)
    shot(win, "single.png")

    # ALL OFF
    win.mode_box.setCurrentIndex(2); win._mode_changed(2)
    win._all_on(); pump(app, 0.5)
    assert all(c.on for c in sim.model.ch.values())
    win._all_off(); pump(app, 0.5)
    assert not any(c.on for c in sim.model.ch.values())

    # sequence via dialog
    win._open_sequence()
    d = win.seq_dialog
    d.file_edit.setText(os.path.join(ROOT, "examples", "slow_square.csv")); d._load()
    assert len(d.steps) == 2
    d.repeat.setValue(2); d._start()
    pump(app, 1.5)
    assert "running" in d.state.text() and win.panels[1].seq_lbl.isVisible() is not None
    shot(d, "sequence.png")
    pump(app, 3.0)
    assert "finished" in d.state.text()

    # CSV recording
    win.rec_btn.setChecked(True); win._toggle_record(True)
    pump(app, 1.0)
    win.rec_btn.setChecked(False); win._toggle_record(False)
    assert any(f.endswith(".csv") for f in os.listdir(os.path.join(cfg.log_dir, "data")))

    # config persisted
    win.close()
    import json
    data = json.load(open(cfg.path))
    pr = data["supplies"]["DP8SIM000001"]["channels"]["1"]["presets"]
    assert pr[0]["v"] == 5.0 and len(pr) == 5
    assert data["ui"]["view_mode"] == 3


def test_rename_and_reconnect(app, env, monkeypatch):
    sim, cfg, win = env
    pump(app, 1.0)
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Bench PSU", True))
    win._rename_supply()
    assert cfg.supplies["DP8SIM000001"]["name"] == "Bench PSU"
    assert "Bench PSU" in win.supply_box.currentText()
    sim.kill_clients()
    pump(app, 4.0)
    assert win.worker.connected          # auto-reconnect
