```
pip install git+https://github.com/lis-analytics/pqdif2csv
pqdif2csv meter_week.pqd                       # -> meter_week_csv/  (one CSV per observation)
pqdif2csv meter_week.pqd --layout long         # one row per value, easy to filter or pivot
pqdif2csv meter_week.pqd --excel week.xlsx     # also write an Excel workbook
pqdif2csv meter_week.pqd --summary             # list observations, channels and series
```

For Excel output: `pip install "pqdif2csv[excel] @ git+https://github.com/lis-analytics/pqdif2csv"`

Not yet on PyPI.

## What you get

- **Wide CSV** (default): one file per observation and time axis. Columns are `time_utc`,
  `seconds_from_start`, then one column per series, named from the file's own metadata,
  for example `PST1 [AN] FLKR_PST VAL`. Quantities logged on different intervals (10-minute
  Pst and 2-hour Plt, say) go to separate files rather than being forced onto one axis.
- **Long CSV**: observation, time, channel, phase, quantity, characteristic, value type,
  units and value on every row.
- **Excel**: one sheet per wide table. Tables longer than Excel's row limit are listed
  and skipped.
- **manifest.json**: file summary, SHA-256 of the input, the reader version and the
  conventions used, and every series that was not exported with the reason.

From Python:

```python
from pqdif2csv import PqdifFile, convert
pf = PqdifFile("meter_week.pqd")
for obs in pf.observations:
    print(obs["name"], obs["time_start_utc"], len(obs["channels"]))
convert("meter_week.pqd", "out", layout="both")
```

## Decisions you should know about

- **Dates.** PQDIF counts days from 1899-12-30, as the reference implementations do. The
  standard's text says "January 1, 1900", which would shift every date by two days.
- **Scaling.** Scale and offset are applied only when the series is stored as scaled.
- **CT/PT ratios.** Applied only when the file's monitor settings say to use transducer
  ratios and give both sides; each channel records whether a ratio was applied.
- **Nothing is dropped silently.** Read errors, checksum failures and unexported series are
  all listed in the manifest.

Not supported: the deprecated whole-file compression style, and interpretation of
vendor-specific quantities beyond their names. This is a reader and converter; it does not
check a file against every rule of IEEE 1159.3, and it makes no assessment of the data.

## Validation

- 18 tests: synthetic files with known values (compression on and off, scaled storage,
  increment time axes, shared series, transducer ratios, damaged files), plus public
  sample files when downloaded with `python tests/fetch_samples.py`.
- Cross-checked against an independent Python reader,
  [ShietFak/pqdif](https://github.com/ShietFak/pqdif), with `tools/crosscheck.py`: on the
  sample files it can open, 2,644 series and 2,555,824 values, 0 mismatches. (It could not
  open the two IEEE example files.)

Please report a file that reads wrongly by opening an issue; if you can share the file or a
trimmed copy, include it.

## Credits and licence

MIT licence. Tag and identifier constants are generated from the Gemstone PQDIF library
(Grid Protection Alliance, MIT); see NOTICE.md. The IEEE SA OPEN PQDIFNet and Gemstone PQDIF
implementations were used as references for details the standard leaves open.

Provided as is, without warranty. Built by [LIS Analytics](https://lisanalytics.net), which
analyzes recorded power quality data for facility owners and engineering firms.
