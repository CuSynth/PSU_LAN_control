"""Sequence ("slow signal generator") files.

Text/CSV file, one step per line::

    # duration, voltage, current        <- comments and header are ignored
    2.0,  1.0, 0.5
    500ms, 2.5, -          <- '-' or empty = keep the current value
    1 min, 5.0, 0.5

Separators: comma, semicolon, tab or whitespace.  Duration may carry a unit
(ms, s, min, m, h); a bare number means seconds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class SeqStep:
    duration: float            # seconds
    v: Optional[float] = None  # None = leave unchanged
    i: Optional[float] = None


class SequenceFileError(ValueError):
    pass


_UNITS = {"ms": 1e-3, "s": 1.0, "sec": 1.0, "m": 60.0, "min": 60.0, "h": 3600.0}
_DUR_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)\s*([a-zA-Z]*)\s*$")
_BLANK = {"", "-", "keep", "nan", "none", "*"}


def parse_duration(text: str) -> float:
    m = _DUR_RE.match(text)
    if not m:
        raise ValueError(f"bad duration {text!r}")
    unit = m.group(2).lower() or "s"
    if unit not in _UNITS:
        raise ValueError(f"unknown time unit {unit!r}")
    return float(m.group(1)) * _UNITS[unit]


def _parse_val(text: str) -> Optional[float]:
    t = text.strip().lower().rstrip("va").strip()
    if t in _BLANK:
        return None
    return float(t)


def parse_sequence_text(text: str) -> list[SeqStep]:
    steps: list[SeqStep] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "," in line or ";" in line or "\t" in line:
            parts = [p.strip() for p in re.split(r"[,;\t]", line)]
        else:
            # whitespace separated; a duration like "1 min" has an inner space
            parts = line.split()
            if len(parts) == 4 and parts[1].lower() in _UNITS:
                parts = [parts[0] + parts[1], parts[2], parts[3]]
        if len(parts) < 2:
            raise SequenceFileError(f"line {n}: need at least duration and voltage")
        try:
            dur = parse_duration(parts[0])
        except ValueError as e:
            if not steps and n <= 3:   # header row such as "time_s,voltage,current"
                continue
            raise SequenceFileError(f"line {n}: {e}") from None
        try:
            v = _parse_val(parts[1])
            i = _parse_val(parts[2]) if len(parts) > 2 else None
        except ValueError as e:
            raise SequenceFileError(f"line {n}: {e}") from None
        if dur <= 0:
            raise SequenceFileError(f"line {n}: duration must be > 0")
        if (v is not None and v < 0) or (i is not None and i < 0):
            raise SequenceFileError(f"line {n}: negative values are not allowed")
        if v is None and i is None:
            raise SequenceFileError(f"line {n}: both voltage and current are empty")
        steps.append(SeqStep(dur, v, i))
    if not steps:
        raise SequenceFileError("no steps found in file")
    return steps


def load_sequence_file(path: str) -> list[SeqStep]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return parse_sequence_text(f.read())


def total_duration(steps: list[SeqStep]) -> float:
    return sum(s.duration for s in steps)
