# DP832 Control

Python + Qt (PySide6/pyqtgraph) control app for LAN power supplies (raw SCPI sockets):

| family | models | LAN port |
|---|---|---|
| Rigol DP800 | DP832 (3 ch; DP821/DP811 by model name) | 5555 |
| R&S / HAMEG HMP | **HMP4040 (4 ch), HMP4030 (3 ch)**; HMP2030/2020 use the same driver, untested | 5025 |

```
pip install -r requirements.txt
python main.py                     # --config path.json, --debug (log every SCPI line)
python tools/dp832_sim.py          # hardware-free DP832 simulator on 127.0.0.1:5555 (then "Find / add" -> Probe IP 127.0.0.1)
python tools/dp832_sim.py --model HMP4040      # HMP simulator on :5025 (--model HMP4030, --load 10,10,5,2)
pytest tests                       # worker + UI tests (offscreen) against the simulator
```

## Architecture

| package | role |
|---|---|
| `dp832_worker/` | **Qt-free** library. `PsuWorker` owns the socket in one thread, picks an instrument driver (`drivers.py`: `DP800Driver`, `HMPDriver`; capabilities in `models.py`) from `*IDN?`, takes commands through a priority queue (`submit()` / helpers return a `Ticket`), polls telemetry, runs sequences with drift-free timing, auto-reconnects. Events go to listeners (`telemetry`, `connection`, `sequence`, `alarm`, `error`). Also `discover()`, `DataLogger`, sequence-file parser. |
| `dp832_ui/` | Qt app. `WorkerBridge` re-emits worker events in the GUI thread; everything else is widgets + `config.py` (JSON). |

```python
from dp832_worker import PsuWorker
w = PsuWorker("192.168.1.50"); w.add_listener(print); w.start()
w.set_vi(1, v=5.0, i=0.5).get(2); w.set_output(1, True)
```

## Features / behaviour

* **Discovery**: scans the local IPv4 subnets (capped to a /24 for big nets) for TCP 5555 and 5025 + `*IDN?`; or probe a typed IP. Supplies are keyed by serial number, so a DHCP change of IP is picked up automatically.
* **Config** `dp832_config.json` (next to `main.py`): supplies (IP, name), last V/I, limits, 5 presets per channel, view mode, update rate, plot span, window geometry, sequence-dialog settings. Saved atomically.
* **Supply selector + Rename**; one supply is connected at a time (switching never touches outputs).
* **Views**: Single / Dual / Triple (/ Quad for HMP4040). Click an inactive CH button to swap it in. Only visible channels are polled (faster updates for the ones you watch).
* **Plots** per channel: V and I, each with dashed *set* and solid *real*; update rate (0.2-20 Hz, limited by the instrument's LAN speed; measured rate shown bottom right) and span are configurable.
* **Limits** dialog: *software* max V/I (enforced by the worker on every setpoint - manual, preset and sequence) plus instrument OVP/OCP level + enable, and "clear trip".
* **Presets**: 5 slots per channel per supply (V, I, software limits, OVP/OCP). **Loading always switches the channel OFF first** (done inside the worker, before anything else), sets values, leaves it OFF.
* **ON/OFF** per channel, **ALL ON** (asks for confirmation) / **ALL OFF** (urgent priority, jumps the queue).
* **Logging**: rotating `logs/dp832.log`, live log dock, and *Record CSV* (`logs/data/data_<supply>_<time>.csv`: set/measured V, I, P, output, mode).
* **Sequence player** (slow signal generator): text/CSV, one step per line `duration, voltage, current`
  (`#` comments, header ignored, `-` = keep value, durations like `500ms`, `2 s`, `1 min`).
  Pick channel, repeat count or loop forever, output on at start / off at end. See `examples/`.
  Steps use absolute deadlines, so timing doesn't drift. Values above the software limit are clamped (with a prompt).
  Pressing a channel's OFF mid-sequence keeps the output off while steps continue; the Stop button ends it.

## HMP4040 / HMP4030 specifics

* The HMP selects a channel (`INST:NSEL n`) and all `VOLT/CURR/MEAS/OUTP:SEL/FUSE/VOLT:PROT` commands act on it; the driver re-selects before every channel operation, so front-panel channel changes can't misdirect commands.
* A channel drives its output only when **OUTP:SEL** (per channel) **and** the master **OUTP:GEN** are on. The app maps: channel OFF = `SEL OFF`; channel ON = `SEL ON` + `GEN ON`, *after deselecting other channels that are selected-but-idle* so they don't switch on with the master; **ALL OFF = `OUTP:GEN OFF`** (one command); ALL ON selects all + `GEN ON`.
* **OVP** is always active (no enable); **"OCP" is the electronic fuse**: enable + delay in ms (shown as "Fuse delay" in Limits; trip badge reads FUSE). The fuse has no remote clear - it re-arms when the channel is switched on again.
* CV/CC comes from `STAT:QUES:INST:ISUM<n>:COND?`; per-channel output state from `OUTP:SEL?` + `OUTP:GEN?` (falls back to `OUTP?`). Features the unit doesn't answer are detected once at connect and skipped.
* Measurement has no power query, so P = V x I.

## Safety notes

* Closing the app or switching supply **leaves outputs as they are**. Lost link: sequences stop, outputs stay as last set.
* Last V/I are shown but **not** re-applied on connect unless you set `"apply_last_on_connect": true` (then output stays OFF).
* SCPI follows the Rigol DP800 programming guide (`:SOURce<n>:VOLTage`, `:OUTPut CH1,ON`, `:MEASure:ALL?`, `:OUTPut:MODE?`, `:OUTPut:OVP/OCP...`).
  The DP800 and HMP drivers have been tested against the bundled simulators only (HMP commands follow the HAMEG/R&S HMP SCPI manual) - please try them on your unit with a dummy load first.
