"""Export a read PQDIF file to CSV (wide or long) and, optionally, Excel."""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import re

import numpy as np

from .reader import PqdifFile

EXCEL_MAX_ROWS = 1_048_575          # one row is used by the header

_PREFIXES = ("ID_SERIES_VALUE_TYPE_", "ID_QC_", "ID_QU_", "ID_PHASE_", "ID_QM_", "ID_QT_")


def short(v):
    """'ID_QC_FLKR_PST' -> 'FLKR_PST'. Unknown values are returned as text."""
    if v is None:
        return ""
    v = str(v)
    for p in _PREFIXES:
        if v.startswith(p):
            return v[len(p):]
    return v


def _iso(t0, rel):
    """ISO time of t0 + rel, computed without losing sub-millisecond precision."""
    base = _dt.datetime.fromtimestamp(float(t0), tz=_dt.timezone.utc)
    return (base + _dt.timedelta(seconds=float(rel))).isoformat(timespec="microseconds")


def _safe(name, n=40):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "observation")).strip("_")
    return (s or "observation")[:n]


def _is_time(s):
    return s["value_type"] == "ID_SERIES_VALUE_TYPE_TIME"


def _numeric(v):
    return isinstance(v, np.ndarray) and v.dtype.kind in "iufb" and v.ndim == 1


def _complex(v):
    return isinstance(v, np.ndarray) and v.dtype.kind == "c" and v.ndim == 1


def _out_values(s):
    """Series values for output. Values stored as 32-bit floats and not rescaled are
    written at that precision (101.25433, not 101.25432586669922)."""
    v = s["values"]
    if s.get("stored_type") == 40 and isinstance(v, np.ndarray) and v.dtype.kind == "f":
        return v.astype(np.float32)
    return v


def column_name(ch, s):
    parts = [ch["name"] or f"channel{ch['definition_index']}"]
    if ch.get("phase") not in (None, "ID_PHASE_NONE"):
        parts.append(f"[{short(ch['phase'])}]")
    parts.append(short(s["characteristic"]))
    parts.append(short(s["value_type"]))
    u = short(s["units"])
    if u and u != "NONE":
        parts.append(f"({u})")
    return " ".join(p for p in parts if p)


def _times(obs, ch):
    """(t0, seconds after t0) for a channel's samples, or None. t0 is the observation start,
    or the first sample when times are stored as absolute timestamps and no start is given."""
    for s in ch["series"]:
        if _is_time(s) and _numeric(s["values"]):
            t = s["values"].astype(float)
            t0 = obs["time_start_epoch_s"]
            if s.get("absolute_time"):
                if t0 is None:
                    if not t.size:
                        return None
                    t0 = float(t[0])
                return float(t0), t - t0
            return (None if t0 is None else (float(t0), t))
    return None


def groups(pf):
    """Yield (obs_index, obs, times, [(column, values)], skipped) with one group per
    distinct time axis in each observation."""
    for oi, obs in enumerate(pf.observations):
        buckets, skipped = {}, []
        for ch in obs["channels"]:
            tt = _times(obs, ch)
            t0, t = tt if tt else (None, None)
            for s in ch["series"]:
                if _is_time(s):
                    continue
                v = _out_values(s)
                col = column_name(ch, s)
                if v is None:
                    skipped.append({"column": col, "reason": "no values"})
                    continue
                if not (_numeric(v) or _complex(v)):
                    skipped.append({"column": col, "reason": "not a numeric series"})
                    continue
                if t is None:                      # untimed series: export against a sample index
                    if not len(v):
                        skipped.append({"column": col, "reason": "empty series"})
                        continue
                    key = ("untimed", len(v))
                    b = buckets.setdefault(key, {"t0": None, "t": np.arange(len(v), dtype=float), "cols": []})
                    names = {c for c, _ in b["cols"]}
                    name, k = col, 2
                    while name in names:
                        name, k = f"{col} #{k}", k + 1
                    b["cols"].append((name, v))
                    continue
                if len(t) == 0:
                    skipped.append({"column": col, "reason": "empty series"})
                    continue
                if len(v) != len(t):
                    skipped.append({"column": col, "reason": f"{len(v)} values but {len(t)} times"})
                    continue
                key = (t0, len(t), hash(np.asarray(t).tobytes()))
                b = buckets.setdefault(key, {"t0": t0, "t": t, "cols": []})
                names = {c for c, _ in b["cols"]}
                name, k = col, 2
                while name in names:
                    name, k = f"{col} #{k}", k + 1
                b["cols"].append((name, v))
        for gi, b in enumerate(buckets.values(), 1):
            yield oi, obs, gi, (b["t0"], b["t"]), b["cols"], skipped if gi == 1 else []
        if not buckets:
            yield oi, obs, 0, None, [], skipped


def _fmt(x):
    if isinstance(x, (complex, np.complexfloating)):
        return f"{x.real!r}{x.imag:+r}j"
    if isinstance(x, np.floating) and x.dtype.itemsize < 8:
        # shortest text that round-trips at the stored precision (101.25433, not 101.25432586669922)
        if x == 0 or 1e-4 <= abs(x) < 1e15:
            return np.format_float_positional(x, unique=True, trim="-")
        return np.format_float_scientific(x, unique=True, trim="-")
    if isinstance(x, (np.floating, float)):
        return repr(float(x))
    if isinstance(x, (np.bool_, bool)):
        return int(x)
    return x


def write_wide(pf, outdir):
    """One CSV per observation and time axis: time_utc, then one column per series."""
    os.makedirs(outdir, exist_ok=True)
    files, skipped = [], []
    for oi, obs, gi, tt, cols, sk in groups(pf):
        for s in sk:
            skipped.append({"observation": oi, "observation_name": obs["name"], **s})
        if tt is None or not cols:
            continue
        t0, t = tt
        fn = (f"obs{oi + 1:03d}_{_safe(obs['name'])}" + (f"_axis{gi}" if gi > 1 else "")
              + ("_untimed" if t0 is None else "") + ".csv")
        path = os.path.join(outdir, fn)
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            if t0 is None:
                w.writerow(["sample_index"] + [c for c, _ in cols])
                for i in range(len(t)):
                    w.writerow([i] + [_fmt(v[i]) for _, v in cols])
            else:
                w.writerow(["time_utc", "seconds_from_start"] + [c for c, _ in cols])
                for i in range(len(t)):
                    w.writerow([_iso(t0, t[i]), repr(float(t[i]))] + [_fmt(v[i]) for _, v in cols])
        files.append({"file": fn, "observation": oi, "observation_name": obs["name"],
                      "rows": int(len(t)), "columns": [c for c, _ in cols]})
    return files, skipped


def write_long(pf, path):
    """A single tidy CSV: one row per value."""
    n, skipped = 0, []
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["observation", "observation_name", "time_utc", "channel", "phase", "measured",
                    "quantity_type", "characteristic", "value_type", "units", "value"])
        for oi, obs in enumerate(pf.observations):
            for ch in obs["channels"]:
                tt = _times(obs, ch)
                t0, t = tt if tt else (None, None)
                for s in ch["series"]:
                    if _is_time(s):
                        continue
                    v = s["values"]
                    col = column_name(ch, s)
                    if v is not None and (_numeric(v) or _complex(v)) and t is None:
                        meta = [ch["name"], short(ch["phase"]), short(ch["measured"]), short(ch["quantity_type"]),
                                short(s["characteristic"]), short(s["value_type"]), short(s["units"])]
                        for i in range(len(v)):
                            w.writerow([oi + 1, obs["name"], ""] + meta + [_fmt(v[i])])
                        n += len(v)
                        continue
                    if v is None or not (_numeric(v) or _complex(v)) or len(v) != len(t):
                        skipped.append({"observation": oi, "observation_name": obs["name"], "column": col})
                        continue
                    meta = [ch["name"], short(ch["phase"]), short(ch["measured"]), short(ch["quantity_type"]),
                            short(s["characteristic"]), short(s["value_type"]), short(s["units"])]
                    for i in range(len(v)):
                        w.writerow([oi + 1, obs["name"], _iso(t0, t[i])] + meta + [_fmt(v[i])])
                    n += len(v)
    return n, skipped


def write_excel(pf, path):
    """One sheet per observation and time axis. Needs openpyxl."""
    try:
        from openpyxl import Workbook
    except ImportError as e:          # pragma: no cover
        raise SystemExit("Excel output needs openpyxl:  pip install openpyxl") from e
    wb = Workbook(write_only=True)
    used, sheets, too_big = set(), [], []
    for oi, obs, gi, tt, cols, _ in groups(pf):
        if tt is None or not cols:
            continue
        t0, t = tt
        if len(t) > EXCEL_MAX_ROWS:
            too_big.append({"observation": oi, "rows": int(len(t))})
            continue
        base = f"{oi + 1:03d}_{_safe(obs['name'], 22)}" + (f"_{gi}" if gi > 1 else "")
        name, k = base[:31], 2
        while name in used:
            name, k = f"{base[:28]}_{k}", k + 1
        used.add(name)
        ws = wb.create_sheet(name)
        ws.append((["sample_index"] if t0 is None else ["time_utc", "seconds_from_start"]) + [c for c, _ in cols])
        for i in range(len(t)):
            row = [i] if t0 is None else [_iso(t0, t[i]), float(t[i])]
            for _, v in cols:
                x = v[i]
                row.append(str(_fmt(x)) if np.iscomplexobj(x) else (float(x) if v.dtype.kind == "f" else int(x)))
            ws.append(row)
        sheets.append(name)
    wb.save(path)
    return sheets, too_big


def convert(pqd, outdir, layout="wide", excel=None):
    pf = PqdifFile(pqd)
    os.makedirs(outdir, exist_ok=True)
    result = {"input": os.path.basename(pqd), "provenance": pf.provenance(), "summary": pf.summary()}
    if layout in ("wide", "both"):
        result["wide_files"], result["skipped"] = write_wide(pf, outdir)
    if layout in ("long", "both"):
        p = os.path.join(outdir, _safe(os.path.splitext(os.path.basename(pqd))[0], 60) + "_long.csv")
        n, sk = write_long(pf, p)
        result["long_file"] = {"file": os.path.basename(p), "rows": n}
        result.setdefault("skipped", sk)
    if excel:
        result["excel"] = {"file": excel, **dict(zip(("sheets", "too_big_for_excel"), write_excel(pf, excel)))}
    with open(os.path.join(outdir, "manifest.json"), "w") as fh:
        json.dump(result, fh, indent=1, default=str)
    return result
