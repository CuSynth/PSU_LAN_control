# DP832 Control

Python + Qt (PySide6/pyqtgraph) control app for Rigol DP800-series supplies over LAN (raw SCPI, TCP 5555).

```
pip install -r requirements.txt
python main.py                     # --config path.json, --debug (log every SCPI line)
python tools/dp832_sim.py          # hardware-free simulator on 127.0.0.1:5555 (then "Find / add" -> Probe IP 127.0.0.1)
pytest tests                       # worker + UI tests (offscreen) against the simulator
```

## Architecture

| package | role |
|---|---|
| `dp832_worker/` | **Qt-free** library. `PsuWorker` owns the socket in one thread, takes commands through a priority queue (`submit()` / helpers return a `Ticket`), polls telemetry, runs sequences with drift-free timing, auto-reconnects. Events go to listeners (`telemetry`, `connection`, `sequence`, `alarm`, `error`). Also `discover()`, `DataLogger`, sequence-file parser. |
| `dp832_ui/` | Qt app. `WorkerBridge` re-emits worker events in the GUI thread; everything else is widgets + `config.py` (JSON). |

```python
from dp832_worker import PsuWorker
w = PsuWorker("192.168.1.50"); w.add_listener(print); w.start()
w.set_vi(1, v=5.0, i=0.5).get(2); w.set_output(1, True)
```

## Features / behaviour

* **Discovery**: scans the local IPv4 subnets (capped to a /24 for big nets) for TCP 5555 + `*IDN?`; or probe a typed IP. Supplies are keyed by serial number, so a DHCP change of IP is picked up automatically.
* **Config** `dp832_config.json` (next to `main.py`): supplies (IP, name), last V/I, limits, 5 presets per channel, view mode, update rate, plot span, window geometry, sequence-dialog settings. Saved atomically.
* **Supply selector + Rename**; one supply is connected at a time (switching never touches outputs).
* **Views**: Single / Dual / Triple. Click an inactive CH button to swap it in. Only visible channels are polled (faster updates for the ones you watch).
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

## Safety notes

* Closing the app or switching supply **leaves outputs as they are**. Lost link: sequences stop, outputs stay as last set.
* Last V/I are shown but **not** re-applied on connect unless you set `"apply_last_on_connect": true` (then output stays OFF).
* SCPI follows the Rigol DP800 programming guide (`:SOURce<n>:VOLTage`, `:OUTPut CH1,ON`, `:MEASure:ALL?`, `:OUTPut:MODE?`, `:OUTPut:OVP/OCP...`).
  It has been tested against the bundled simulator only - please try it on your unit with a dummy load first.
