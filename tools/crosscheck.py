"""Compare pqdif2csv's decoded values with an independent reader, ShietFak/pqdif
(https://github.com/ShietFak/pqdif, MIT, Python 3.11+), on the files in tests/data.

    pip install git+https://github.com/ShietFak/pqdif
    python tests/fetch_samples.py
    python tools/crosscheck.py

Transducer (CT/PT) ratios are applied by pqdif2csv and not by the other reader, so they
are applied to its values before comparing. Files the other reader cannot open are
reported, not counted."""
import glob
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pqdif as P                                   # noqa: E402
from pqdif2csv.reader import PqdifFile              # noqa: E402

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "data")


def main():
    tot_s = tot_v = tot_m = 0
    for f in sorted(glob.glob(os.path.join(HERE, "*.pqd"))):
        a = PqdifFile(f)
        try:
            b = P.read(f, validate=False)
        except Exception as e:                     # noqa: BLE001
            print(f"{os.path.basename(f):32s} other reader could not open it: {type(e).__name__}")
            continue
        ns = nv = nm = 0
        for oa, ob in zip(a.observations, b.observations):
            for ca, cb in zip(oa["channels"], ob.channels):
                m = re.match(r"transducer ratio ([0-9.eE+-]+)", ca["transducer"])
                ratio = float(m.group(1)) if m else 1.0
                for sa, sb in zip(ca["series"], cb.series):
                    va = sa["values"]
                    if not (isinstance(va, np.ndarray) and va.dtype.kind in "iufb") or sa.get("absolute_time"):
                        continue
                    vb = np.asarray(sb.to_numpy(), dtype=float)
                    if sa["value_type"] != "ID_SERIES_VALUE_TYPE_TIME":
                        vb = vb * ratio
                    ns, nv = ns + 1, nv + len(va)
                    if len(va) != len(vb) or not np.allclose(va.astype(float), vb, rtol=1e-9, atol=1e-12,
                                                              equal_nan=True):
                        nm += 1
        print(f"{os.path.basename(f):32s} series {ns:5d}  values {nv:9d}  mismatches {nm}")
        tot_s, tot_v, tot_m = tot_s + ns, tot_v + nv, tot_m + nm
    print(f"TOTAL series {tot_s}  values {tot_v}  mismatches {tot_m}")
    return 1 if tot_m else 0


if __name__ == "__main__":
    sys.exit(main())
