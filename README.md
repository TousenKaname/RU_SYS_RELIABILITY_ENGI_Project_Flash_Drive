# RU_SYS_RELIABILITY_ENGI_Project_Flash_Drive

**flashrel** runs and analyzes an endurance test of USB flash drives under repeated
copy → verify → delete cycling. It is the test software of our project for
16:540:585 Systems Reliability Engineering I (Rutgers, Fall 2026); the test plan lives in
the companion report repository (`Project/`).

- **Test harness**: drives many USB drives in parallel (one process per drive), fills
  each drive to 90 % of its free space with files of a given size class, reads every
  byte back *around the host's page cache*, deletes the files, and logs every cycle.
  It detects data corruption, I/O errors, disconnects and hangs, read-only locks and
  file-system faults, separates intermittent from hard failures, and helps tell port
  faults from drive faults.
- **Analysis**: life table, Kaplan–Meier, Weibull and lognormal MLE with censoring,
  Weibayes bounds, Weibull regression (brand, capacity), a per-cycle proportional-hazards
  model (file size), degradation pseudo-failure times, the mean cumulative function, and
  an Excel workbook organized by brand, capacity and file size.

The harness needs only Python ≥ 3.10 and PyYAML, so it runs on Windows, macOS and Linux
(including a Raspberry Pi). The analysis adds NumPy, SciPy, pandas, Matplotlib and openpyxl.

## Quick start

```bash
pip install -e ".[analysis]"          # add ,sensors for the Arduino thermometer, ,dev for tests

flashrel selftest                     # cache bypass + fault detection work on this computer?
flashrel enroll configs/phase1.yaml --drive A8-03 --mount E:\     # once per drive (exFAT, empty)
flashrel run configs/phase1.yaml --host W                         # all drives assigned to host W
flashrel status configs/phase1.yaml                               # progress of every drive
flashrel export configs/phase1.yaml --out data/phase1.xlsx        # Excel workbook
flashrel analyze configs/phase1.yaml --out reports/phase1         # fits, tables, figures
```

On macOS use the volume path as the mount, e.g. `--mount /Volumes/A8-03`. `run` keeps the
computer awake, staggers the workers by 30 s, prints a status table every minute and
stops cleanly on Ctrl+C (the cycle in progress is discarded and repeated on restart).

## How one cycle works

1. **Locate** the drive by its identity file (`FLASHREL-ID.json`), never by drive letter.
2. **Plan** a reproducible file set for cycle *k*: the workload (small / medium / large)
   follows a fixed rotation; sizes are log-uniform; space is counted in whole clusters;
   the set fills 90 % of the free space and never the last 64 MiB.
3. **Write** (host → drive): each file is generated in memory and flushed with `fsync`
   (`F_FULLFSYNC` on macOS). No source file ever touches the host disk.
4. **Read back** (drive → host) bypassing the page cache (`FILE_FLAG_NO_BUFFERING` on
   Windows; `msync(MS_INVALIDATE)` plus `F_NOCACHE` on macOS; `posix_fadvise` on Linux).
   On macOS and Linux the block reads of every read-back are counted, so a read-back
   served from memory is flagged in the log. Each file is compared byte for byte with
   the regenerated reference. Every 1 MiB chunk carries a 32-byte header (file key,
   cycle, file and chunk number), so a bad chunk is classified as bit errors, stale data,
   misdirected data, an erased page, garbled or truncated.
5. **Delete** the files and check that the free space comes back.

Errors are retried twice (5 s, 10 s). Cleared errors are *transient* (D3); persistent
ones end the cycle with a failure code, after which the worker waits for the drive to
re-enumerate, checks for a read-only lock and runs a 64 MiB recovery check.

| Code | Meaning | Code | Meaning |
| --- | --- | --- | --- |
| F1 | data corruption (incl. missing / truncated files) | F5 | port or host fault (not counted) |
| F2 | unrecoverable I/O error | F6 | file-system fault (space, capacity) |
| F3 | disconnect, or no I/O progress for 10 min (hang) | D1 | write throughput < 50 % of baseline |
| F4 | read-only lock | D2 | intermittent failure (works after reset) |
| | | D3 | transient error cleared by a retry |

A drive's life ends (hard failure) when it does not pass the recovery protocol, locks
read-only, changes capacity, or has 3 cycle failures within 10 cycles. See
[`docs/operator_guide.md`](docs/operator_guide.md) for the operator protocol.

## Repository layout

```text
configs/            inventory.yaml (drives), phase1.yaml (campaign), probes.example.yaml
src/flashrel/       the package
  config.py         campaign YAML -> validated dataclasses
  inventory.py      drive groups and unit IDs
  payload.py        deterministic file plans and self-describing chunks (SHAKE-128)
  verify.py         byte-exact comparison and corruption diagnosis
  cycle.py          one write / read-back / delete cycle with retries
  failure.py        failure taxonomy, events, drive state, degradation rules
  recorder.py       append-only CSV / JSON Lines logs, resumable state, drive locks
  runner.py         per-drive worker, recovery protocol, supervisor, hang watchdog
  intake.py         enrollment and capacity (counterfeit) screening
  sensors.py        temperature sources (Arduino over serial)
  system/           OS layer: cache-bypassing I/O, volume discovery, error mapping, sleep
  analysis/         life data, nonparametric, parametric, regression, per-cycle hazard,
                    degradation, attribution, planning, simulation, export, report
  viz/              figure style, data plots, schematic diagrams
scripts/            make_plan_figures.py, make_plan_tables.py, planning_assumptions.py
hardware/arduino/   DS18B20 temperature logger sketch
docs/               operator guide
tests/              68 tests, including end-to-end cycles with injected faults
```

Each drive's logs live in `<data_dir>/<campaign>/<drive>/`: `cycles.csv` (one row per
cycle, 36 fields), `events.jsonl`, `state.json` (resume point) and `intake.json`. The
logs are the record of truth: a restarted worker reconciles `state.json` with them, so
no cycle is counted twice. Point `data_dir` in the campaign file at a synced folder so
both hosts write to one place.

## Figures and tables of the test plan

```bash
python scripts/make_plan_figures.py --out ../Project/figures --formats pdf
python scripts/make_plan_tables.py  --out ../Project/tables
```

The figures follow one visual system (`viz/style.py`): 7 pt sans-serif type, thin axes,
brand as hue and capacity as lightness, colour-blind-safe, and exported at exactly the
IEEE text width so LaTeX places them at 1:1 scale. The tables are generated from the same
YAML files the harness runs on. Planning speeds are in `scripts/planning_assumptions.py`;
replace them with the pilot's measurements and regenerate.

`flashrel simulate configs/phase1.yaml --out data-sim` writes synthetic logs (made-up
parameters, clearly not measurements) to rehearse the whole analysis before real data exist.

## Development

```bash
pip install -e ".[analysis,dev]"
pytest
```

Team: Guoan Wang, Shangqing Wei, Mengtong Ai.
