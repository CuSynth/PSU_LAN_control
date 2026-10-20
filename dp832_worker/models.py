"""Per-model capabilities: channel count, default ranges, LAN port, protection flavour."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PORT_RIGOL = 5555      # Rigol raw SCPI socket
PORT_HAMEG = 5025      # R&S / HAMEG HMP (HO720/HO730 LAN) raw SCPI socket
ALL_PORTS = (PORT_RIGOL, PORT_HAMEG)


@dataclass(frozen=True)
class ModelProfile:
    family: str                          # "DP800" | "HMP"
    model: str
    n_channels: int
    ranges: tuple                        # ((vmax, imax), ...) one entry per channel
    port: int
    ovp_enable: bool = True              # False: OVP is always active (HMP)
    ovp_max: Optional[float] = None      # None: 1.1 x channel vmax
    ocp_kind: str = "level"              # "level" (DP800 OCP) | "fuse" (HMP electronic fuse)
    ocp_label: str = "OCP level"
    ocp_unit: str = "A"
    ocp_decimals: int = 4
    ocp_max: Optional[float] = None      # None: 1.1 x channel imax
    ocp_step: float = 0.01
    ocp_enable_text: str = "OCP enabled"

    def range(self, ch: int) -> tuple:
        return self.ranges[min(ch, len(self.ranges)) - 1]


_DP_RANGES = ((32.0, 3.2), (32.0, 3.2), (5.3, 3.2))
_HMP_R = (32.05, 10.01)


def _dp(model: str, n: int) -> ModelProfile:
    return ModelProfile("DP800", model, n, _DP_RANGES[:n], PORT_RIGOL)


def _hmp(model: str, n: int) -> ModelProfile:
    return ModelProfile(
        "HMP", model, n, (_HMP_R,) * n, PORT_HAMEG,
        ovp_enable=False, ovp_max=32.5,
        ocp_kind="fuse", ocp_label="Fuse delay", ocp_unit="ms", ocp_decimals=0, ocp_max=250.0,
        ocp_step=10.0,
        ocp_enable_text="Electronic fuse enabled (trips when the current limit is reached for the delay)")


_HMP = {"HMP4040": 4, "HMP4030": 3, "HMP2030": 3, "HMP2020": 2}   # 2020/2030: same SCPI family
_DP_N = {"DP811": 1, "DP811A": 1, "DP821": 2, "DP821A": 2}


def profile_for(model: str) -> ModelProfile:
    m = (model or "").upper().strip()
    if m in _HMP:
        return _hmp(m, _HMP[m])
    return _dp(m or "DP832", _DP_N.get(m, 3))        # default: the DP800 dialect (as before)
