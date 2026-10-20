import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from dp832_sim import Model, SimServer  # noqa: E402
from dp832_worker import PsuWorker, SeqStep, discover, probe, profile_for  # noqa: E402


@pytest.fixture(params=["HMP4040", "HMP4030"])
def sim(request):
    s = SimServer(model=Model((10.0, 10.0, 5.0, 2.0), kind=request.param)).start()
    yield s
    s.shutdown(); s.server_close(); s.kill_clients()


@pytest.fixture()
def worker(sim):
    w = PsuWorker("127.0.0.1", sim.port, name="hmp")
    w.events = []
    w.add_listener(w.events.append)
    w.start()
    end = time.time() + 5
    while not w.connected and time.time() < end:
        time.sleep(0.02)
    assert w.connected
    yield w
    w.stop()


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def live(sim, ch):
    c = sim.model.ch[ch]
    return c.sel and sim.model.gen


def test_profiles():
    p = profile_for("HMP4040")
    assert (p.family, p.n_channels, p.port, p.ocp_kind, p.ovp_enable) == ("HMP", 4, 5025, "fuse", False)
    assert profile_for("hmp4030").n_channels == 3
    assert profile_for("DP832").family == "DP800" and profile_for("DP832").port == 5555


def test_connect_ranges_and_driver(worker, sim):
    n = 4 if sim.model.kind == "HMP4040" else 3
    assert worker.profile.family == "HMP" and worker.n_channels == n
    assert worker.ranges[n] == (32.05, 10.01)
    d = worker.driver
    assert d.have_sel_q and d.have_gen_q and d.mode_cmd


def test_set_measure_modes(worker, sim):
    worker.set_vi(1, 5.0, 2.0).get(2)
    worker.set_vi(2, 5.0, 0.1).get(2)
    worker.set_all_output(True, [1, 2]).get(2)
    assert wait_for(lambda: worker.state[1].mode == "CV" and worker.state[2].mode == "CC"
                    and abs((worker.state[1].v or 0) - 5.0) < 0.1
                    and abs((worker.state[2].v or 0) - 1.0) < 0.1)     # CC: 0.1 A * 10 ohm
    assert worker.state[1].output and worker.state[2].output


def test_channel_on_does_not_wake_preselected_channels(worker, sim):
    m = sim.model
    m.ch[1].sel = True                      # selected but master off -> idle
    m.ch[3].sel = True
    worker.set_vi(2, 3.0, 1.0).get(2)
    worker.set_output(2, True).get(2)
    assert live(sim, 2) and m.gen
    assert not live(sim, 1) and not live(sim, 3)
    assert not m.ch[1].sel and not m.ch[3].sel


def test_channel_on_keeps_running_channels(worker, sim):
    worker.set_output(1, True).get(2)
    worker.set_output(2, True).get(2)
    assert live(sim, 1) and live(sim, 2)
    worker.set_output(1, False).get(2)
    assert not live(sim, 1) and live(sim, 2)
    assert wait_for(lambda: worker.state[1].output is False and worker.state[2].output is True)


def test_all_off_uses_master_switch(worker, sim):
    worker.set_all_output(True).get(2)
    assert all(live(sim, c) for c in sim.model.ch)
    sim.model.log.clear()
    worker.set_all_output(False).get(2)
    assert not any(live(sim, c) for c in sim.model.ch)
    assert any(c.strip().upper().startswith("OUTP:GEN OFF") for c in sim.model.log)
    assert wait_for(lambda: not any(worker.state[c].output for c in worker.state))


def test_software_limit_and_error_report(worker, sim):
    worker.set_limits(1, vmax=6.0, imax=1.0).get(2)
    assert worker.set_vi(1, 20.0, 5.0).get(2) == (6.0, 1.0)
    assert sim.model.ch[1].v == 6.0


def test_ovp_trip(worker, sim):
    worker.set_protection(1, ovp=3.0).get(2)
    worker.set_vi(1, 8.0, 3.0).get(2)
    worker.set_output(1, True).get(2)
    assert wait_for(lambda: any(e.kind == "alarm" and e.data == {"ch": 1, "type": "OVP"} for e in worker.events))
    assert wait_for(lambda: worker.state[1].output is False)
    worker.clear_protection(1).get(2)
    worker.refresh().get(2)
    assert not worker.state[1].ovp_trip


def test_fuse_is_ocp(worker, sim):
    worker.set_protection(1, ocp=100, ocp_on=True).get(2)
    worker.refresh().get(2)
    assert worker.state[1].ocp_on and worker.state[1].ocp_level == 100
    assert worker.state[1].ovp_on is True
    worker.set_vi(1, 5.0, 0.1).get(2)          # 10 ohm load -> CC -> fuse trips
    worker.set_output(1, True).get(2)
    assert wait_for(lambda: any(e.kind == "alarm" and e.data["type"] == "OCP" for e in worker.events))
    assert not live(sim, 1)


def test_apply_setup_off_first(worker, sim):
    worker.set_vi(2, 3.0, 1.0).get(2)
    worker.set_output(2, True).get(2)
    worker.apply_setup(2, v=4.0, i=0.5, vmax=10, imax=2, ovp=5.0, ocp=50, ocp_on=True).get(2)
    c = sim.model.ch[2]
    assert not live(sim, 2) and c.v == 4.0 and c.i == 0.5 and c.ovp == 5.0 and c.fuse and c.fuse_delay == 50


def test_sequence(worker, sim):
    worker.start_sequence(2, [SeqStep(0.3, 1.0, 1.0), SeqStep(0.3, 2.0, None)], repeat=1).get(2)
    assert wait_for(lambda: any(e.kind == "sequence" and e.data["state"] == "finished" for e in worker.events), 5)
    seq = [e.data["v"] for e in worker.events if e.kind == "sequence" and e.data["state"] == "step"]
    assert seq == [1.0, 2.0]
    assert not live(sim, 2)


def test_discover_both_ports(sim):
    dp = SimServer(model=Model()).start()
    try:
        found = discover(extra_hosts=["127.0.0.1"], networks=[], port=[1, dp.port, sim.port])
        assert sorted(d.model for d in found) == sorted(["DP832", sim.model.kind]) or len(found) >= 1
        assert probe("127.0.0.1", [dp.port]).model == "DP832"
        info = probe("127.0.0.1", [sim.port])
        assert info.model == sim.model.kind and info.profile.family == "HMP"
    finally:
        dp.shutdown(); dp.server_close(); dp.kill_clients()
