"""Download the public PQDIF sample files used by the optional tests into tests/data.

The files are not redistributed in this repository; each stays under its own licence.
SHA-256 hashes pin the exact versions the tests were written against."""
import hashlib
import os
import urllib.parse
import urllib.request

RAW = "https://raw.githubusercontent.com"
SAMPLES = {
    # local name: (url, sha256, source and licence)
    "example-fault.pqd": (f"{RAW}/namnh240795/pqdif-reader/main/examples/example-fault.pqd",
                          "44897267254442c8d7b7ad0dc9253daab2840bc68da125c10442c76c2fd4b66e",
                          "IEEE PQDIF examples via namnh240795/pqdif-reader, Apache-2.0"),
    "example-flicker.pqd": (f"{RAW}/namnh240795/pqdif-reader/main/examples/example-flicker.pqd",
                            "ee92c32af6bb9dbc3d77a81403d2c28845f5dd26720c230b37ef9ea097f64361",
                            "IEEE PQDIF examples via namnh240795/pqdif-reader, Apache-2.0"),
    "unipower_up2210_10kV_week.pqd": (
        f"{RAW}/namnh240795/pqdif/main/" + urllib.parse.quote("40-6084 PQDIFExport_Utility S 10kV-v1.pqd"),
        "f405dd171fae7fa5fa75d32ea0ab8564a87daba03cf6321b83bd0faf35afcbfc",
        "namnh240795/pqdif, MIT"),
    "gemstone-none.pqd": (f"{RAW}/ShietFak/pqdif/main/tests/interop/fixtures/gemstone-none.pqd",
                          "16dc7752dd1ab5bdce74de33039f79d8a6a9a35f3f9ccd28732621b35d7ebf0f",
                          "ShietFak/pqdif (written by Gemstone PQDIF), MIT"),
    "gemstone-zlib.pqd": (f"{RAW}/ShietFak/pqdif/main/tests/interop/fixtures/gemstone-zlib.pqd",
                          "724994858f2eeb6877126f2372b110ac42969e2e81ad4f512daeaef8d55bcffa",
                          "ShietFak/pqdif (written by Gemstone PQDIF), MIT"),
}


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out, exist_ok=True)
    for name, (url, sha, src) in SAMPLES.items():
        data = urllib.request.urlopen(url, timeout=60).read()
        got = hashlib.sha256(data).hexdigest()
        status = "ok" if got == sha else f"HASH MISMATCH (got {got[:12]}...)"
        with open(os.path.join(out, name), "wb") as fh:
            fh.write(data)
        print(f"{name:34s} {len(data):>8d} bytes  {status}  [{src}]")


if __name__ == "__main__":
    main()
