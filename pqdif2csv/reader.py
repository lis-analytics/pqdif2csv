"""
pqdif2csv.reader -- read PQDIF (IEEE Std 1159.3) files in pure Python.

Physical format (from IEEE 1159.3 and the open-source reference implementations:
IEEE SA OPEN PQDIFNet, Apache-2.0; Gemstone PQDIF, MIT):
  record header 64 bytes: signature GUID, record-type tag GUID, header size (int32),
  body size (int32), next-record position (int32, absolute), Adler-32 checksum (uint32),
  16 reserved bytes. Body: a collection element; with record-level compression the
  body is zlib-compressed. Elements: tag GUID (16), element type (1 = collection,
  2 = scalar, 3 = vector), physical type, embedded flag, reserved byte, then 8 bytes of
  embedded scalar value or a (link int32, size int32) pair, links relative to the body.
  All little-endian. GUIDs use the Windows mixed-endian byte order (uuid bytes_le).

Conventions (recorded in every file's provenance):
  - Timestamps: days are counted from 1899-12-30 (the OLE automation epoch). The
    standard's text says "days since January 1, 1900", but its own constant
    EXCEL_DAYCOUNT_ADJUST = 25569 and the reference implementations put day 0 at
    1899-12-30. Decoding from 1900-01-01 shifts every date by two days.
  - Increment storage: runs of (count, increment) after a leading run count; each run
    continues from where the previous one ended.
  - Scale/offset are applied only when the series definition's storage method has the
    "scaled" bit. Transducer (CT/PT) ratios from a monitor-settings record are applied
    only when that record says UseTransducer and the channel setting gives both ratios;
    whether they were applied is reported per channel.

Not supported: the deprecated total-file compression style, full validation against
every rule of IEEE 1159.3, and interpretation of vendor-specific quantities. Anything
that cannot be read is reported in the file's errors, never dropped silently.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import json
import math
import os
import struct
import sys
import uuid
import zlib

import numpy as np

from . import tags as _T

READER_NAME = "pqdif2csv"
READER_VERSION = "0.1.0"
OLE_EPOCH = _dt.datetime(1899, 12, 30, tzinfo=_dt.timezone.utc)
_OLE_EPOCH_S = OLE_EPOCH.timestamp()

TAG = {k: uuid.UUID(v) for k, v in _T.TAGS.items()}
_TAG_NAME = {v: k for k, v in TAG.items()}


def _ids(group):
    out = {}
    for name, val in _T.VALUES.get(group, {}).items():
        try:
            out[uuid.UUID(val)] = name
        except ValueError:
            out[int(val, 0)] = name
    return out


QUANTITY_TYPE = _ids("tagQuantityTypeID")
CHARACTERISTIC = _ids("tagQuantityCharacteristicID")
VALUE_TYPE = _ids("tagValueTypeID")
PHASE = _ids("tagPhaseID")
MEASURED = _ids("tagQuantityMeasuredID")
UNITS = _ids("tagQuantityUnitsID")

# physical type code -> (struct format or None, byte size)
_PTYPE = {1: ("<?", 1), 2: ("<H", 2), 3: ("<I", 4), 10: ("<c", 1), 11: ("<H", 2),
          20: ("<b", 1), 21: ("<h", 2), 22: ("<i", 4), 30: ("<B", 1), 31: ("<H", 2),
          32: ("<I", 4), 40: ("<f", 4), 41: ("<d", 8), 42: (None, 8), 43: (None, 16),
          50: (None, 12), 60: (None, 16)}
_NP = {1: "u1", 2: "<u2", 3: "<u4", 20: "i1", 21: "<i2", 22: "<i4", 30: "u1", 31: "<u2",
       32: "<u4", 40: "<f4", 41: "<f8"}

COMP_STYLE_NONE, COMP_STYLE_TOTALFILE, COMP_STYLE_RECORDLEVEL = 0, 1, 2
COMP_ALG_NONE, COMP_ALG_ZLIB = 0, 1
MAX_DEPTH = 64


class PqdifError(ValueError):
    """The file cannot be read as PQDIF."""


def ole_to_epoch(days, seconds):
    return _OLE_EPOCH_S + float(days) * 86400.0 + float(seconds)


def _iso(t):
    return _dt.datetime.fromtimestamp(t, tz=_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Physical layer
# ---------------------------------------------------------------------------
class Element:
    __slots__ = ("tag", "etype", "ptype", "value", "children")

    def __init__(self, tag, etype, ptype, value=None, children=None):
        self.tag, self.etype, self.ptype = tag, etype, ptype
        self.value, self.children = value, children

    @property
    def tag_name(self):
        return _TAG_NAME.get(self.tag, str(self.tag))

    def find(self, name):
        t = TAG[name]
        for c in self.children or ():
            if c.tag == t:
                return c
        return None

    def find_all(self, name):
        t = TAG[name]
        return [c for c in (self.children or ()) if c.tag == t]

    def get(self, name, default=None):
        c = self.find(name)
        return default if c is None else c.value


def _decode_scalar(ptype, raw):
    fmt, n = _PTYPE[ptype]
    if ptype == 50:
        d, s = struct.unpack("<Id", raw[:12])
        return ole_to_epoch(d, s)
    if ptype == 60:
        return uuid.UUID(bytes_le=bytes(raw[:16]))
    if ptype == 42:
        return complex(*struct.unpack("<ff", raw[:8]))
    if ptype == 43:
        return complex(*struct.unpack("<dd", raw[:16]))
    v = struct.unpack(fmt, raw[:n])[0]
    return v.decode("latin-1") if ptype == 10 else v


def _decode_vector(ptype, buf, pos, n):
    size = _PTYPE[ptype][1]
    end = pos + n * size
    if n < 0 or end > len(buf):
        raise PqdifError("vector runs past the end of the record body")
    raw = buf[pos:end]
    if ptype in _NP:
        a = np.frombuffer(raw, dtype=_NP[ptype]).copy()
        return a.astype(bool) if ptype in (1, 2, 3) else a
    if ptype == 10:
        return raw.split(b"\0", 1)[0].decode("latin-1")
    if ptype == 11:
        return raw.decode("utf-16-le").split("\0", 1)[0]
    if ptype == 42:
        a = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
        return a[:, 0] + 1j * a[:, 1]
    if ptype == 43:
        a = np.frombuffer(raw, dtype="<f8").reshape(-1, 2)
        return a[:, 0] + 1j * a[:, 1]
    if ptype == 50:
        return np.array([ole_to_epoch(*struct.unpack_from("<Id", raw, 12 * i)) for i in range(n)])
    if ptype == 60:
        return [uuid.UUID(bytes_le=bytes(raw[16 * i:16 * i + 16])) for i in range(n)]
    raise PqdifError(f"unsupported physical type {ptype}")


def _read_collection(buf, pos, depth, errors):
    if depth > MAX_DEPTH:
        raise PqdifError("collection nesting too deep")
    if pos + 4 > len(buf):
        raise PqdifError("collection header past end of body")
    n = struct.unpack_from("<i", buf, pos)[0]
    if n < 0 or pos + 4 + 28 * n > len(buf):
        raise PqdifError(f"collection claims {n} elements that do not fit in the body")
    items = []
    for i in range(n):
        p = pos + 4 + 28 * i
        tag = uuid.UUID(bytes_le=bytes(buf[p:p + 16]))
        etype, ptype, embedded = buf[p + 16], buf[p + 17], buf[p + 18]
        try:
            if etype == 2 and embedded:
                if ptype not in _PTYPE:
                    raise PqdifError(f"unknown physical type {ptype}")
                items.append(Element(tag, 2, ptype, _decode_scalar(ptype, buf[p + 20:p + 28])))
                continue
            link = struct.unpack_from("<i", buf, p + 20)[0]
            if link < 0 or link >= len(buf):
                raise PqdifError(f"element link {link} outside the record body")
            if etype == 1:
                items.append(Element(tag, 1, 0, children=_read_collection(buf, link, depth + 1, errors)))
            elif etype == 2:
                if ptype not in _PTYPE:
                    raise PqdifError(f"unknown physical type {ptype}")
                size = _PTYPE[ptype][1]
                if link + size > len(buf):
                    raise PqdifError("scalar past end of body")
                items.append(Element(tag, 2, ptype, _decode_scalar(ptype, buf[link:link + size])))
            elif etype == 3:
                if ptype not in _PTYPE:
                    raise PqdifError(f"unknown physical type {ptype}")
                cnt = struct.unpack_from("<i", buf, link)[0]
                items.append(Element(tag, 3, ptype, _decode_vector(ptype, buf, link + 4, cnt)))
            else:
                raise PqdifError(f"unknown element type {etype}")
        except (PqdifError, struct.error, ValueError) as e:
            errors.append({"element_tag": _TAG_NAME.get(tag, str(tag)), "error": str(e)})
    return items


def read_records(path, max_records=100000):
    """Yield (header dict, root Element) for each record. Raises PqdifError for a
    file that is not PQDIF at all; element-level problems are collected in errors."""
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 64:
        raise PqdifError("file is shorter than one PQDIF record header")
    pos, seen = 0, set()
    style, alg = COMP_STYLE_NONE, COMP_ALG_NONE
    records, errors = [], []
    while 0 <= pos < len(data) and pos not in seen and len(records) < max_records:
        seen.add(pos)
        if pos + 64 > len(data):
            errors.append({"record_pos": pos, "error": "truncated record header"})
            break
        sig = uuid.UUID(bytes_le=data[pos:pos + 16])
        rtype = uuid.UUID(bytes_le=data[pos + 16:pos + 32])
        hsize, bsize, nxt, cks = struct.unpack_from("<iiiI", data, pos + 32)
        if not records and rtype != TAG["tagContainer"]:
            raise PqdifError("first record is not a PQDIF container record")
        body_start = pos + hsize if hsize >= 64 else pos + 64
        body = data[body_start:body_start + bsize]
        hdr = {"pos": pos, "signature": str(sig), "type": _TAG_NAME.get(rtype, str(rtype)),
               "header_size": hsize, "body_size": bsize, "next": nxt, "checksum": cks}
        if len(body) != bsize:
            errors.append({"record_pos": pos, "error": "record body truncated"})
            break
        hdr["checksum_ok"] = cks == 0 or (zlib.adler32(body) & 0xFFFFFFFF) == cks
        hdr["checksum_provided"] = cks != 0
        rec_err = []
        try:
            if records and style == COMP_STYLE_TOTALFILE:
                raise PqdifError("total-file compression is deprecated and not supported")
            if records and style == COMP_STYLE_RECORDLEVEL and alg == COMP_ALG_ZLIB and bsize:
                body = zlib.decompress(body)
            root = Element(rtype, 1, 0, children=_read_collection(bytes(body), 0, 0, rec_err)) \
                if bsize else Element(rtype, 1, 0, children=[])
        except (PqdifError, zlib.error, struct.error) as e:
            errors.append({"record_pos": pos, "record_type": hdr["type"], "error": str(e)})
            root = None
        for e in rec_err:
            e["record_pos"] = pos
        errors.extend(rec_err)
        if root is not None:
            if not records:
                style = int(root.get("tagCompressionStyleID", 0) or 0)
                alg = int(root.get("tagCompressionAlgorithmID", 0) or 0)
                hdr["compression"] = {"style": style, "algorithm": alg}
            records.append((hdr, root))
        if nxt <= 0 or nxt >= len(data):
            break
        pos = nxt
    return records, errors, {"style": style, "algorithm": alg}


# ---------------------------------------------------------------------------
# Logical layer
# ---------------------------------------------------------------------------
def _name(v):
    if isinstance(v, np.ndarray):
        v = "".join(chr(int(c)) for c in v if int(c))
    return v.strip("\0 ").strip() if isinstance(v, str) else v


def _series_values(si, sdef, scale_ok=True):
    v = si.get("tagSeriesValues")
    if v is None:
        return None
    method = int(sdef.get("storage_method") or 1)
    if isinstance(v, np.ndarray) and v.dtype.kind in "iuf" and (method & 4):
        n_runs = int(v[0])
        out, start = [], 0.0
        for r in range(n_runs):
            cnt, inc = v[1 + 2 * r], v[2 + 2 * r]
            out.append(start + np.arange(int(cnt)) * float(inc))
            start = start + float(cnt) * float(inc)
        v = np.concatenate(out) if out else np.zeros(0)
    if isinstance(v, np.ndarray) and v.dtype.kind in "iuf":
        v = v.astype(float)
        if method & 2 and scale_ok:
            sc = si.get("tagSeriesScale")
            of = si.get("tagSeriesOffset")
            sc = 1.0 if sc is None else float(np.asarray(sc).ravel()[0])
            of = 0.0 if of is None else float(np.asarray(of).ravel()[0])
            v = of + v * sc
    return v


class PqdifFile:
    def __init__(self, path):
        self.path = path
        self.sha256 = _sha256_file(path)
        recs, self.errors, self.compression = read_records(path)
        self.n_records = len(recs)
        self.checksum_failures = [h["pos"] for h, _ in recs if not h.get("checksum_ok", True)]
        self.container = {}
        self.data_sources, self.settings, self.observations = [], [], []
        ds, ms = None, None
        for hdr, root in recs:
            t = hdr["type"]
            if t == "tagContainer":
                self.container = {"file_name": _name(root.get("tagFileName")),
                                  "creation_utc": _iso(root.get("tagCreation")) if root.get("tagCreation") else None,
                                  "version": [int(x) for x in np.asarray(root.get("tagVersionInfo", []))]}
            elif t == "tagRecDataSource":
                ds = self._data_source(root)
                self.data_sources.append(ds)
            elif t == "tagRecMonitorSettings":
                ms = self._settings(root)
                self.settings.append(ms)
            elif t == "tagRecObservation":
                if ds is None:
                    self.errors.append({"record_pos": hdr["pos"], "error": "observation before any data source"})
                    continue
                self.observations.append(self._observation(root, ds, ms, hdr["pos"]))

    @staticmethod
    def _data_source(root):
        chans = []
        cd = root.find("tagChannelDefns")
        for c in (cd.find_all("tagOneChannelDefn") if cd else []):
            sdefs = []
            sd = c.find("tagSeriesDefns")
            for s in (sd.find_all("tagOneSeriesDefn") if sd else []):
                sdefs.append({"value_type": VALUE_TYPE.get(s.get("tagValueTypeID"), str(s.get("tagValueTypeID"))),
                              "units": UNITS.get(s.get("tagQuantityUnitsID"), s.get("tagQuantityUnitsID")),
                              "characteristic": CHARACTERISTIC.get(s.get("tagQuantityCharacteristicID"),
                                                                   str(s.get("tagQuantityCharacteristicID"))),
                              "storage_method": s.get("tagStorageMethodID"),
                              "nominal": s.get("tagSeriesNominalQuantity")})
            chans.append({"name": _name(c.get("tagChannelName")),
                          "phase": PHASE.get(c.get("tagPhaseID"), c.get("tagPhaseID")),
                          "quantity_type": QUANTITY_TYPE.get(c.get("tagQuantityTypeID"), str(c.get("tagQuantityTypeID"))),
                          "measured": MEASURED.get(c.get("tagQuantityMeasuredID"), c.get("tagQuantityMeasuredID")),
                          "group_id": c.get("tagChannelGroupID"),
                          "series": sdefs})
        return {"name": _name(root.get("tagNameDS")), "model": _name(root.get("tagInstrumentModelName")),
                "serial": _name(root.get("tagSerialNumberDS")), "version": _name(root.get("tagVersionDS")),
                "vendor_id": str(root.get("tagVendorID")) if root.get("tagVendorID") else None,
                "equipment_id": str(root.get("tagEquipmentID")) if root.get("tagEquipmentID") else None,
                "channels": chans}

    @staticmethod
    def _settings(root):
        chs = {}
        arr = root.find("tagChannelSettingsArray")
        for c in (arr.find_all("tagOneChannelSetting") if arr else []):
            idx = c.get("tagChannelDefnIdx")
            if idx is not None:
                chs[int(idx)] = {"system_side": c.get("tagXDSystemSideRatio"),
                                 "monitor_side": c.get("tagXDMonitorSideRatio")}
        return {"use_transducer": bool(root.get("tagUseTransducer", False)),
                "nominal_frequency": root.get("tagNominalFrequency"), "channels": chs}

    @staticmethod
    def _observation(root, ds, ms, pos):
        t0 = root.get("tagTimeStart")
        chans_out, warn = [], []
        ci = root.find("tagChannelInstances")
        insts = ci.find_all("tagOneChannelInst") if ci else []
        raw = []
        for c in insts:
            idx = c.get("tagChannelDefnIdx")
            if idx is None or int(idx) >= len(ds["channels"]):
                warn.append(f"channel instance with bad definition index {idx}")
                raw.append(None)
                continue
            si = c.find("tagSeriesInstances")
            raw.append((int(idx), c, si.find_all("tagOneSeriesInstance") if si else []))
        for k, item in enumerate(raw):
            if item is None:
                continue
            idx, c, sis = item
            cdef = ds["channels"][idx]
            ratio, ratio_note = 1.0, "no transducer ratio applied"
            if ms and ms["use_transducer"] and idx in ms["channels"]:
                cs = ms["channels"][idx]
                if cs["system_side"] and cs["monitor_side"]:
                    ratio = float(cs["system_side"]) / float(cs["monitor_side"])
                    ratio_note = f"transducer ratio {ratio:g} applied from monitor settings"
            series = []
            for j, s in enumerate(sis):
                sdef = cdef["series"][j] if j < len(cdef["series"]) else {}
                vals, vp = None, None
                sc, ss = s.get("tagSeriesShareChannelIdx"), s.get("tagSeriesShareSeriesIdx")
                if s.find("tagSeriesValues") is not None:
                    vals = _series_values(s, sdef)
                    vp = s.find("tagSeriesValues").ptype
                elif sc is not None and ss is not None:
                    try:
                        oidx, oc, osis = raw[int(sc)]
                        osdef = ds["channels"][oidx]["series"][int(ss)]
                        vals = _series_values(osis[int(ss)], osdef)
                        vp = osis[int(ss)].find("tagSeriesValues").ptype
                    except (TypeError, IndexError):
                        warn.append(f"channel {cdef['name']} series {j}: unresolved shared series")
                if (isinstance(vals, np.ndarray) and vals.dtype.kind == "f" and ratio != 1.0
                        and sdef.get("value_type") != "ID_SERIES_VALUE_TYPE_TIME"):
                    vals = vals * ratio
                series.append({"value_type": sdef.get("value_type"), "units": sdef.get("units"),
                               "characteristic": sdef.get("characteristic"), "values": vals,
                               "absolute_time": vp == 50, "stored_type": vp})
            chans_out.append({"definition_index": idx, "name": cdef["name"], "phase": cdef["phase"],
                              "measured": cdef["measured"], "quantity_type": cdef["quantity_type"],
                              "group_id": c.get("tagChannelGroupID", cdef["group_id"]),
                              "transducer": ratio_note, "series": series})
        return {"record_pos": pos, "name": _name(root.get("tagObservationName")),
                "time_start_epoch_s": t0, "time_start_utc": _iso(t0) if t0 else None,
                "data_source": ds["name"], "channels": chans_out, "warnings": warn}

    # ------------------------------------------------------------------
    def summary(self):
        obs = []
        for o in self.observations:
            obs.append({"name": o["name"], "time_start_utc": o["time_start_utc"],
                        "channels": [{"name": c["name"], "phase": c["phase"], "measured": c["measured"],
                                      "quantity_type": c["quantity_type"],
                                      "series": [(s["value_type"], s["characteristic"],
                                                  None if s["values"] is None else len(s["values"]))
                                                 for s in c["series"]]} for c in o["channels"]]})
        return {"file": os.path.basename(self.path), "sha256": self.sha256,
                "container": self.container, "compression": self.compression,
                "n_records": self.n_records, "n_observations": len(self.observations),
                "checksum_failures": self.checksum_failures, "errors": self.errors[:50],
                "n_errors": len(self.errors), "observations": obs}

    def provenance(self):
        return {"reader": {"name": READER_NAME, "version": READER_VERSION, "sha256": _SELF_SHA256},
                "input": {"name": os.path.basename(self.path), "sha256": self.sha256},
                "conventions": {"timestamp_epoch": "1899-12-30T00:00:00Z (OLE automation date)",
                                "increment_storage": "cumulative runs after a leading run count",
                                "scale_offset": "applied only when storage method has the scaled bit"},
                "read_errors": len(self.errors), "checksum_failures": len(self.checksum_failures)}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_SELF_SHA256 = _sha256_file(os.path.abspath(__file__))

