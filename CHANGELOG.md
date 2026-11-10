# Changelog

## 0.1.0 (2026-11-10)
First public release.
- Pure-Python PQDIF (IEEE 1159.3) reader: record-level zlib compression, scaled and
  increment storage, shared series, transducer (CT/PT) ratios, damaged-file reporting.
- `pqdif2csv` command: wide CSV (one file per observation and time axis), long CSV (one
  row per value), optional Excel, and a manifest.json with a summary, provenance and
  every series that was not exported and why.
