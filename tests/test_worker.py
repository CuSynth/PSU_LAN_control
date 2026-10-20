import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from dp832_sim import Model, SimServer  # noqa: E402
from dp832_worker import (DataLogger, InstrumentError, PsuWorker, SeqStep,  # noqa: E402
                          SequenceFileError, discover, parse_sequence_text, probe)


@pytest.fixture()
def sim():
    s = SimServer(model=Model((10.0, 10.0, 5.0))).start()
    yield s
    s.shutdown()
    s.server_close()


@pytest.fixture()
def worker(sim):
    w = PsuWorker("127.0.0.1", sim.port, name="sim")
    events = []
    w.add_listener(events.append)
    w.events = events
    w.start()
    deadline = time.time() + 5
    while not w.connected and time.time() < deadline:
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


def test_probe_and_discover(sim):
    info = probe("127.0.0.1", sim.port)
    assert info and info.model == "DP832" and info.serial == "DP8SIM000001"
    found = discover(extra_hosts=["127.0.0.1"], networks=[], port=sim.port)
    assert [d.serial for d in found] == ["DP8SIM000001"]
    assert probe("127.0.0.1", 1, timeout=0.2) is None


def test_connect_info(worker):
    assert worker.info.model == "DP832"
    assert worker.n_channels == 3
    assert worker.ranges[3] == (5.3, 3.2)


def test_set_and_output(worker, sim):
    worker.set_vi(1, 5.0, 0.2).get(2)
    worker.set_output(1, True).get(2)
    assert wait_for(lambda: any(
        e.kind == "telemetry" and e.data["channels"][1].output and
        abs(e.data["channels"][1].v - 2.0) < 0.05 for e in worker.events))   # CC: 0.2A*10ohm
    ch = sim.model.ch[1]
    assert ch.on and ch.v == 5.0
    st = [e for e in worker.events if e.kind == "telemetry"][-1].data["channels"][1]
    assert st.mode in ("CC", "CV")
    worker.set_all_output(False).get(2)
    assert not ch.on


def test_software_limit_clamps(worker, sim):
    worker.set_limits(1, vmax=6.0, imax=1.0).get(2)
    res = worker.set_vi(1, 20.0, 2.5).get(2)
    assert res == (6.0, 1.0)
    assert sim.model.ch[1].v == 6.0


def test_instrument_error_reported(worker):
    t = worker.scpi(":SOURce1:VOLTage 99")
    t.get(2)                              # raw write -> no ticket error
    with pytest.raises(InstrumentError):
        worker.set_vi(1, 1.0, 0.1).get(2)   # stale -113/-222 in queue is reported


def test_apply_setup_turns_output_off(worker, sim):
    worker.set_vi(2, 3.0, 1.0).get(2)
    worker.set_output(2, True).get(2)
    assert sim.model.ch[2].on
    worker.apply_setup(2, v=4.0, i=0.5, vmax=10, imax=2, ovp=5.0, ovp_on=True, ocp=1.0, ocp_on=True).get(2)
    ch = sim.model.ch[2]
    assert not ch.on and ch.v == 4.0 and ch.i == 0.5
    assert ch.ovp == 5.0 and ch.ovp_on and ch.ocp_on
    # off must be the first thing that happened
    cmds = [c for c in sim.model.log if "OUTP" in c.upper() and "CH2" in c.upper() and "," in c]
    assert cmds[-1].strip().upper().endswith("OFF") or "VOLT" not in cmds[-1]


def test_ovp_trip_alarm(worker, sim):
    worker.set_protection(1, ovp=3.0, ovp_on=True).get(2)
    worker.set_vi(1, 8.0, 3.0).get(2)
    worker.set_output(1, True).get(2)
    assert wait_for(lambda: any(e.kind == "alarm" and e.data["type"] == "OVP" for e in worker.events))
    assert wait_for(lambda: not worker.state[1].output)
    worker.clear_protection(1).get(2)
    worker.refresh().get(2)
    assert not worker.state[1].ovp_trip


def test_sequence_timing(worker, sim):
    steps = [SeqStep(0.3, 1.0, 0.5), SeqStep(0.3, 2.0, None), SeqStep(0.3, 3.0, 0.5)]
    worker.start_sequence(1, steps, repeat=1, output_on_start=True, output_off_end=True).get(2)
    assert wait_for(lambda: any(e.kind == "sequence" and e.data["state"] == "finished" for e in worker.events), 5)
    seq = [(e.data["step"], e.data["v"]) for e in worker.events if e.kind == "sequence" and e.data["state"] == "step"]
    assert seq == [(0, 1.0), (1, 2.0), (2, 3.0)]
    assert not sim.model.ch[1].on


def test_sequence_step_period_accuracy(worker):
    steps = [SeqStep(0.25, 1.0, 0.5), SeqStep(0.25, 2.0, 0.5)]
    stamps = []
    worker.add_listener(lambda e: e.kind == "sequence" and e.data["state"] == "step"
                        and stamps.append(time.monotonic()))
    worker.start_sequence(1, steps, repeat=4).get(2)
    assert wait_for(lambda: any(e.kind == "sequence" and e.data["state"] == "finished" for e in worker.events), 5)
    d = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 8
    assert all(abs(x - 0.25) < 0.08 for x in d), d
    assert abs((stamps[-1] - stamps[0]) - 7 * 0.25) < 0.1      # no cumulative drift


def test_sequence_stop_and_loop(worker):
    worker.start_sequence(1, [SeqStep(0.1, 1.0, 0.5)], repeat=0).get(2)
    time.sleep(0.5)
    assert worker.sequence_running(1)
    worker.stop_sequence(1, output_off=True).get(2)
    assert not worker.sequence_running(1)


def test_disconnect_and_reconnect(sim):
    w = PsuWorker("127.0.0.1", sim.port, reconnect_s=0.2)
    ev = []
    w.add_listener(ev.append)
    w.start()
    assert wait_for(lambda: w.connected)
    w.start_sequence(1, [SeqStep(5, 1.0, 0.5)]).get(2)
    sim.shutdown(); sim.server_close(); sim.kill_clients()
    assert wait_for(lambda: not w.connected, 5)
    assert not w.sequence_running(1)
    assert any(e.kind == "connection" and e.data["state"] == "disconnected" for e in ev)
    with pytest.raises(Exception):
        w.set_vi(1, 1, 1).get(3)
    w.stop()


def test_datalogger(worker, tmp_path):
    dl = DataLogger(str(tmp_path), "my supply")
    worker.add_listener(dl.on_event)
    path = dl.start()
    time.sleep(0.8)
    dl.stop()
    rows = open(path).read().strip().splitlines()
    assert rows[0].startswith("timestamp_iso") and len(rows) > 3


def test_sequence_parser():
    s = parse_sequence_text("# hdr\ntime_s,voltage,current\n1,2.5,0.5\n500ms; 3 ; -\n1 min 4 0.1\n2\t5\t\n")
    assert [(x.duration, x.v, x.i) for x in s] == [
        (1.0, 2.5, 0.5), (0.5, 3.0, None), (60.0, 4.0, 0.1), (2.0, 5.0, None)]
    for bad in ("", "1,-2,0", "0,1,1", "x,1,1\ny,2,2", "1,-,-"):
        with pytest.raises(SequenceFileError):
            parse_sequence_text(bad)
