import argparse
import json
import os
import sys

from . import __version__
from .export import convert
from .reader import PqdifFile, PqdifError


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pqdif2csv",
                                 description="Read a PQDIF (IEEE 1159.3) file and export it to CSV or Excel.")
    ap.add_argument("pqd", help="input .pqd file")
    ap.add_argument("-o", "--out", help="output folder (default: <input name>_csv)")
    ap.add_argument("--layout", choices=["wide", "long", "both"], default="wide",
                    help="wide: one CSV per observation and time axis (default); long: one row per value")
    ap.add_argument("--excel", metavar="FILE.xlsx", help="also write an Excel workbook (needs openpyxl)")
    ap.add_argument("--summary", action="store_true", help="print a JSON summary and exit")
    ap.add_argument("--version", action="version", version=f"pqdif2csv {__version__}")
    a = ap.parse_args(argv)
    try:
        if a.summary:
            print(json.dumps(PqdifFile(a.pqd).summary(), indent=1, default=str))
            return 0
        out = a.out or os.path.splitext(a.pqd)[0] + "_csv"
        r = convert(a.pqd, out, a.layout, a.excel)
    except PqdifError as e:
        print(f"pqdif2csv: {a.pqd}: {e}", file=sys.stderr)
        return 2
    s = r["summary"]
    print(f"{s['n_observations']} observations, {s['n_errors']} read errors, "
          f"{len(s['checksum_failures'])} checksum failures")
    for f in r.get("wide_files", []):
        print(f"  {f['file']}: {f['rows']} rows x {len(f['columns'])} series")
    if "long_file" in r:
        print(f"  {r['long_file']['file']}: {r['long_file']['rows']} rows")
    if r.get("skipped"):
        print(f"  {len(r['skipped'])} series not exported (see manifest.json)")
    print(f"Output: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
