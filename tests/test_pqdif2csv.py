"""Tests for pqdif2csv.

1. Known-answer files written here by a small test-only PQDIF writer (synthetic data):
   compression on/off, scaled storage, increment time axis, shared series, transducer
   ratio, value logs with two time axes, damaged files.
2. Optional regression on public sample files: run  python tests/fetch_samples.py  first.
   Tests for files that are not present are skipped.
"""
import csv
import json
import os
import struct
import uuid
import zlib

import numpy as np
import pytest

import pqdif2csv.reader as R
from pqdif2csv import convert
from pqdif2csv.__main__ import main

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
T = {k: uuid.UUID(v) for k, v in R._T.TAGS.items()}
V = R._T.VALUES


def g(group, name):
    return uuid.UUID(V[group][name])


def iv(group, name):
    return int(V[group][name], 0)


# ---------------------------------------------------------------------------
# test-only writer
# ---------------------------------------------------------------------------
def S(tag, ptype, value):
    return ("S", T[tag], ptype, value)


def VEC(tag, ptype, values):
    return ("V", T[tag], ptype, values)


def C(tag, *children):
    return ("C", T[tag], 0, list(children))


def _scalar_bytes(ptype, v):
    if ptype == 60:
        return v.bytes_le
    if ptype == 50:
        days = int(v // 86400)
        return struct.pack("<Id", days, v - days * 86400)
    fmt = {22: "<i", 32: "<I", 41: "<d", 40: "<f", 1: "<?", 3: "<I"}[ptype]
    return struct.pack(fmt, v)


def _vec_bytes(ptype, vals):
    if ptype == 10:
        b = vals.encode("latin-1") + b"\0"
        return struct.pack("<i", len(b)) + b
    fmt = {41: "<f8", 40: "<f4", 22: "<i4", 32: "<u4", 21: "<i2"}[ptype]
    a = np.asarray(vals, dtype=fmt)
    return struct.pack("<i", a.size) + a.tobytes()


def build_body(node):
    out = bytearray()

    def write_coll(children):
        start = len(out)
        out.extend(struct.pack("<i", len(children)))
        out.extend(b"\0" * 28 * len(children))
        for i, ch in enumerate(children):
            kind, tag, ptype, val = ch
            p = start + 4 + 28 * i
            hdr = bytearray(tag.bytes_le)
            if kind == "S":
                raw = _scalar_bytes(ptype, val)
                if len(raw) <= 8:
                    hdr += bytes([2, ptype, 1, 0]) + raw.ljust(8, b"\0")
                else:
                    link = len(out)
                    out.extend(raw)
                    hdr += bytes([2, ptype, 0, 0]) + struct.pack("<ii", link, len(raw))
            elif kind == "V":
                link = len(out)
                b = _vec_bytes(ptype, val)
                out.extend(b)
                hdr += bytes([3, ptype, 0, 0]) + struct.pack("<ii", link, len(b))
            else:
                link = write_coll(val)
                hdr += bytes([1, 0, 0, 0]) + struct.pack("<ii", link, 0)
            out[p:p + 28] = hdr
        return start

    write_coll(node[3])
    return bytes(out)


def write_pqd(path, records, compress=False):
    bodies = []
    for i, rec in enumerate(records):
        b = build_body(rec)
        if compress and i > 0:
            b = zlib.compress(b)
        bodies.append((rec[1], b))
    data = bytearray()
    pos = 0
    sig = uuid.UUID("4a111440-e49f-11cf-9900-505144000000")
    for i, (tag, b) in enumerate(bodies):
        nxt = 0 if i == len(bodies) - 1 else pos + 64 + len(b)
        data += sig.bytes_le + tag.bytes_le + struct.pack("<iiiI", 64, len(b), nxt, zlib.adler32(b)) + b"\0" * 16
        data += b
        pos += 64 + len(b)
    with open(path, "wb") as fh:
        fh.write(data)


OLE0 = R._OLE_EPOCH_S
START = 1794787200.0      # 2026-11-16T00:00:00Z


def ts(t):
    return t - OLE0       # seconds since the OLE epoch (writer splits into days + seconds)


def container(compress):
    return C("tagContainer", VEC("tagVersionInfo", 32, [1, 5, 1, 0]), VEC("tagFileName", 10, "synthetic.pqd"),
             S("tagCreation", 50, ts(START)),
             S("tagCompressionStyleID", 32, 2 if compress else 0),
             S("tagCompressionAlgorithmID", 32, 1 if compress else 0))


def chan_def(name, phase, measured, qtype, series):
    return C("tagOneChannelDefn", VEC("tagChannelName", 10, name), S("tagPhaseID", 32, iv("tagPhaseID", phase)),
             S("tagQuantityTypeID", 60, g("tagQuantityTypeID", qtype)),
             S("tagQuantityMeasuredID", 32, iv("tagQuantityMeasuredID", measured)),
             C("tagSeriesDefns", *series))


def ser_def(vtype, units, char, method=1):
    return C("tagOneSeriesDefn", S("tagValueTypeID", 60, g("tagValueTypeID", vtype)),
             S("tagQuantityUnitsID", 32, iv("tagQuantityUnitsID", units)),
             S("tagQuantityCharacteristicID", 60, g("tagQuantityCharacteristicID", char)),
             S("tagStorageMethodID", 32, method))


TIME_INC = ser_def("ID_SERIES_VALUE_TYPE_TIME", "ID_QU_SECONDS", "ID_QC_INSTANTANEOUS", 4)


def waveform_file(path, fs=7680.0, n=1280, compress=True, scaled=True, ratio=None, share_time=True):
    t = np.arange(n) / fs
    sigs, defs, insts = [], [], []
    k = 0
    for ph, sh in (("ID_PHASE_AN", 0), ("ID_PHASE_BN", -2.0944), ("ID_PHASE_CN", 2.0944)):
        v = 120 * np.sqrt(2) * np.sin(2 * np.pi * 60 * t + sh) + 3 * np.sin(5 * (2 * np.pi * 60 * t + sh))
        i = 10 * np.sqrt(2) * np.sin(2 * np.pi * 60 * t + sh - 0.3)
        for meas, x in (("ID_QM_VOLTAGE", v), ("ID_QM_CURRENT", i)):
            sigs.append((x, meas))
            unit = "ID_QU_VOLTS" if meas == "ID_QM_VOLTAGE" else "ID_QU_AMPS"
            defs.append(chan_def(f"{ph[-2]}{'V' if meas == 'ID_QM_VOLTAGE' else 'I'}", ph, meas, "ID_QT_WAVEFORM",
                                 [TIME_INC, ser_def("ID_SERIES_VALUE_TYPE_VAL", unit, "ID_QC_INSTANTANEOUS",
                                                    3 if scaled else 1)]))
            if scaled:
                sc = float(np.max(np.abs(x)) / 30000.0)
                q = np.round(x / sc).astype(np.int16)
                val = [S("tagSeriesScale", 41, sc), S("tagSeriesOffset", 41, 0.0), VEC("tagSeriesValues", 21, q)]
            else:
                val = [VEC("tagSeriesValues", 41, x)]
            if share_time and k > 0:
                tser = C("tagOneSeriesInstance", S("tagSeriesShareChannelIdx", 32, 0), S("tagSeriesShareSeriesIdx", 32, 0))
            else:
                tser = C("tagOneSeriesInstance", VEC("tagSeriesValues", 41, [1, n, 1.0 / fs]))
            insts.append(C("tagOneChannelInst", S("tagChannelDefnIdx", 32, k),
                           C("tagSeriesInstances", tser, C("tagOneSeriesInstance", *val))))
            k += 1
    recs = [container(compress),
            C("tagRecDataSource", VEC("tagNameDS", 10, "SYNTH-WF"), C("tagChannelDefns", *defs))]
    if ratio:
        recs.append(C("tagRecMonitorSettings", S("tagUseTransducer", 1, True),
                      C("tagChannelSettingsArray", *[C("tagOneChannelSetting", S("tagChannelDefnIdx", 32, j),
                                                       S("tagXDSystemSideRatio", 41, ratio),
                                                       S("tagXDMonitorSideRatio", 41, 1.0)) for j in range(6)])))
    recs.append(C("tagRecObservation", VEC("tagObservationName", 10, "Synthetic event"),
                  S("tagTimeStart", 50, ts(START)), C("tagChannelInstances", *insts)))
    write_pqd(path, recs, compress)
    return sigs


def valuelog_file(path, n=1008, compress=True):
    """One week of 10-min logs: V harmonics in volts (groups 1,5), V THD %, Pst, Plt (2 h), flag."""
    rng = np.random.default_rng(3)
    t10 = 600.0 * np.arange(1, n + 1)          # interval-end stamps relative to START
    t2h = 7200.0 * np.arange(1, n // 12 + 1)
    pst = 0.2 + 0.1 * rng.random(n) + 1.5 * (rng.random(n) > 0.8)   # spikes make the 2 h alignment testable
    plt = np.array([np.cbrt(np.mean(pst[12 * b:12 * b + 12] ** 3)) for b in range(n // 12)])
    v1 = 120.0 + rng.random(n)
    v5 = 2.4 + 0.2 * rng.random(n)
    thd = 100 * v5 / v1
    flag = np.zeros(n)
    flag[100] = 1
    defs, insts = [], []

    def add(name, ph, meas, char, units, vtype, tt, vals, group=None):
        k = len(defs)
        d = chan_def(name, ph, meas, "ID_QT_VALUELOG",
                     [ser_def("ID_SERIES_VALUE_TYPE_TIME", "ID_QU_SECONDS", "ID_QC_INSTANTANEOUS"),
                      ser_def(vtype, units, char)])
        defs.append(d)
        extra = [S("tagChannelGroupID", 32, group)] if group is not None else []
        insts.append(C("tagOneChannelInst", S("tagChannelDefnIdx", 32, k), *extra,
                       C("tagSeriesInstances", C("tagOneSeriesInstance", VEC("tagSeriesValues", 41, tt)),
                         C("tagOneSeriesInstance", VEC("tagSeriesValues", 41, vals)))))

    add("H U1", "ID_PHASE_AN", "ID_QM_VOLTAGE", "ID_QC_SPECTRA_HGROUP", "ID_QU_VOLTS", "ID_SERIES_VALUE_TYPE_AVG", t10, v1, 1)
    add("H U1", "ID_PHASE_AN", "ID_QM_VOLTAGE", "ID_QC_SPECTRA_HGROUP", "ID_QU_VOLTS", "ID_SERIES_VALUE_TYPE_AVG", t10, v5, 5)
    add("THD U1", "ID_PHASE_AN", "ID_QM_VOLTAGE", "ID_QC_TOTAL_THD", "ID_QU_PERCENT", "ID_SERIES_VALUE_TYPE_AVG", t10, thd)
    add("PST1", "ID_PHASE_AN", "ID_QM_VOLTAGE", "ID_QC_FLKR_PST", "ID_QU_NONE", "ID_SERIES_VALUE_TYPE_VAL", t10, pst)
    add("PLT1", "ID_PHASE_AN", "ID_QM_VOLTAGE", "ID_QC_FLKR_PLT", "ID_QU_NONE", "ID_SERIES_VALUE_TYPE_VAL", t2h, plt)
    add("Flagging", "ID_PHASE_NONE", "ID_QM_STATUS", "ID_QC_NONE", "ID_QU_NONE", "ID_SERIES_VALUE_TYPE_VAL", t10, flag)
    add("U1", "ID_PHASE_AN", "ID_QM_VOLTAGE", "ID_QC_RMS", "ID_QU_VOLTS", "ID_SERIES_VALUE_TYPE_AVG", t10, v1)
    recs = [container(compress), C("tagRecDataSource", VEC("tagNameDS", 10, "SYNTH-LOG"), C("tagChannelDefns", *defs)),
            C("tagRecObservation", VEC("tagObservationName", 10, "Week"), S("tagTimeStart", 50, ts(START)),
              C("tagChannelInstances", *insts))]
    write_pqd(path, recs, compress)
    return dict(v1=v1, v5=v5, thd=thd, pst=pst, plt=plt, flag=flag)


# ---------------------------------------------------------------------------
# reader: synthetic known-answer tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("scaled", [False, True])
def test_waveform_round_trip(tmp_path, compress, scaled):
    p = str(tmp_path / "wf.pqd")
    sigs = waveform_file(p, compress=compress, scaled=scaled)
    pf = R.PqdifFile(p)
    assert pf.errors == [] and pf.checksum_failures == []
    assert pf.compression["style"] == (2 if compress else 0)
    o = pf.observations[0]
    assert o["time_start_epoch_s"] == pytest.approx(START, abs=1e-6)
    assert o["time_start_utc"] == "2026-11-16T00:00:00+00:00"
    tol = 1e-12 if not scaled else 2e-4 * max(np.max(np.abs(x)) for x, _ in sigs)
    for c, (x, _) in zip(o["channels"], sigs):
        assert np.allclose(c["series"][1]["values"], x, atol=tol)
        assert np.allclose(c["series"][0]["values"], np.arange(1280) / 7680.0)   # increment + shared


def test_transducer_ratio_applied(tmp_path):
    p = str(tmp_path / "wf.pqd")
    sigs = waveform_file(p, scaled=False, ratio=60.0)
    c = R.PqdifFile(p).observations[0]["channels"][0]
    assert c["transducer"].startswith("transducer ratio 60")
    assert np.allclose(c["series"][1]["values"], 60.0 * sigs[0][0])
    assert np.allclose(c["series"][0]["values"], np.arange(1280) / 7680.0)   # time axis not scaled


def test_not_pqdif(tmp_path):
    p = tmp_path / "x.pqd"
    p.write_bytes(b"\0" * 200)
    with pytest.raises(R.PqdifError):
        R.PqdifFile(str(p))
    p.write_bytes(b"\1" * 10)
    with pytest.raises(R.PqdifError):
        R.PqdifFile(str(p))


def test_truncated_body_reported_not_crashing(tmp_path):
    p = str(tmp_path / "wf.pqd")
    waveform_file(p, compress=False)
    b = open(p, "rb").read()
    open(p, "wb").write(b[:-500])
    pf = R.PqdifFile(p)
    assert pf.errors and pf.observations == []


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def _read(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def test_wide_waveform_export_matches_input(tmp_path):
    p = str(tmp_path / "wf.pqd")
    sigs = waveform_file(p, scaled=False)
    r = convert(p, str(tmp_path / "out"))
    assert len(r["wide_files"]) == 1 and r["wide_files"][0]["rows"] == 1280
    rows = _read(str(tmp_path / "out" / r["wide_files"][0]["file"]))
    hdr = rows[0]
    assert hdr[:2] == ["time_utc", "seconds_from_start"] and len(hdr) == 2 + 6
    assert rows[1][0] == "2026-11-16T00:00:00.000000+00:00"
    assert float(rows[2][1]) == pytest.approx(1 / 7680.0)
    col = np.array([float(x[2]) for x in rows[1:]])
    assert np.allclose(col, sigs[0][0], atol=1e-12)
    assert json.load(open(str(tmp_path / "out" / "manifest.json")))["summary"]["n_observations"] == 1


def test_wide_value_log_splits_time_axes(tmp_path):
    p = str(tmp_path / "log.pqd")
    k = valuelog_file(p)
    r = convert(p, str(tmp_path / "out"))
    by_rows = {f["rows"]: f for f in r["wide_files"]}
    assert set(by_rows) == {1008, 84}                     # 10-min axis and 2-h Plt axis
    assert any("FLKR_PLT" in c for c in by_rows[84]["columns"])
    rows = _read(str(tmp_path / "out" / by_rows[84]["file"]))
    assert rows[1][0] == "2026-11-16T02:00:00.000000+00:00"   # first 2-h stamp
    assert np.allclose([float(x[2]) for x in rows[1:]], k["plt"])


def test_long_export_row_count(tmp_path):
    p = str(tmp_path / "log.pqd")
    valuelog_file(p)
    r = convert(p, str(tmp_path / "out"), layout="long")
    assert r["long_file"]["rows"] == 6 * 1008 + 84
    rows = _read(str(tmp_path / "out" / r["long_file"]["file"]))
    assert len(rows) == 1 + r["long_file"]["rows"]
    assert rows[0][-1] == "value" and rows[1][3] == "H U1"


def test_excel_export(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = str(tmp_path / "log.pqd")
    valuelog_file(p)
    x = str(tmp_path / "log.xlsx")
    r = convert(p, str(tmp_path / "out"), excel=x)
    wb = openpyxl.load_workbook(x, read_only=True)
    assert len(wb.sheetnames) == 2 == len(r["excel"]["sheets"])


def test_cli(tmp_path, capsys):
    p = str(tmp_path / "wf.pqd")
    waveform_file(p)
    assert main([p, "-o", str(tmp_path / "o")]) == 0
    assert "1 observations" in capsys.readouterr().out
    bad = tmp_path / "bad.pqd"
    bad.write_bytes(b"\0" * 100)
    assert main([str(bad)]) == 2


# ---------------------------------------------------------------------------
# optional: public sample files (python tests/fetch_samples.py)
# ---------------------------------------------------------------------------
def _sample(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        pytest.skip(f"{name} not downloaded; run python tests/fetch_samples.py")
    return p


def test_sample_unipower_week(tmp_path):
    pf = R.PqdifFile(_sample("unipower_up2210_10kV_week.pqd"))
    assert pf.errors == [] and pf.checksum_failures == [] and len(pf.observations) == 29
    assert pf.data_sources[0]["model"] == "Unipower UP-2210"
    o = [o for o in pf.observations if o["name"].startswith("Steady State PST")][0]
    assert o["time_start_utc"] == "2024-09-01T23:00:00+00:00"
    r = convert(_sample("unipower_up2210_10kV_week.pqd"), str(tmp_path / "u"))
    assert r["wide_files"]


@pytest.mark.parametrize("name", ["example-fault.pqd", "example-flicker.pqd"])
def test_sample_ieee_examples(tmp_path, name):
    r = convert(_sample(name), str(tmp_path / "x"))
    assert r["summary"]["n_errors"] == 0 and r["wide_files"]


@pytest.mark.parametrize("name", ["gemstone-zlib.pqd", "gemstone-none.pqd"])
def test_sample_gemstone_values(name):
    pf = R.PqdifFile(_sample(name))
    vals = [s["values"] for c in pf.observations[0]["channels"] for s in c["series"]
            if isinstance(s["values"], np.ndarray) and s["value_type"] != "ID_SERIES_VALUE_TYPE_TIME"]
    assert any(np.allclose(v, [0.25, -1.5, 3.75]) for v in vals if v.size == 3)


def test_sample_gemstone_untimed_export(tmp_path):
    r = convert(_sample("gemstone-zlib.pqd"), str(tmp_path / "g"))
    f = [f for f in r["wide_files"] if f["file"].endswith("_untimed.csv")]
    assert f and f[0]["rows"] == 3
    rows = _read(str(tmp_path / "g" / f[0]["file"]))
    assert rows[0][0] == "sample_index"
