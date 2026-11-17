# FlashRel Rig 2

Unattended life testing of USB flash drives under repeated copy → verify → delete
cycling, with a live web dashboard. Test software of Project 4 in 16:540:585 Systems
Reliability Engineering I (Rutgers, Fall 2026), Group 4: Guoan Wang, Shangqing Wei,
Mengtong Ai.

Version 2 replaces the software of run 1 (`flashrel` 0.1, archived with its data in
`../_archive/Project_code_v1_run1/`). It runs **run 2**: 20 drives on two 10-port hubs for
120 hours on one Windows PC. The design is in [`docs/test_plan_run2.md`](docs/test_plan_run2.md),
the runbook (Chinese) in [`docs/operator_guide_zh.md`](docs/operator_guide_zh.md).

## What it does

* **Cycle** (as in run 1): fill 90 % of the free space with large, medium or small files
  in a fixed rotation, flush every file, read every byte back around the host cache
  (`FILE_FLAG_NO_BUFFERING`), compare it with regenerated reference data, delete.
* **New in run 2**: checks of data at rest and of the exFAT file-system records every 10
  minutes and at every phase boundary (identity + sentinel files read uncached; boot-region
  and up-case checksums and root-directory entry sets read raw), so a failure is
  time-stamped when it happens; exact dose counters (bytes, files created and deleted) in
  every record; a recovery protocol with a software USB re-plug and a fresh-data check;
  hub-event detection (F5); hang watchdog; resume after crashes and restarts; post-test
  data-retention check, re-check on another port and port checks with spares.
* **Dashboard** (http://127.0.0.1:8585): both hubs with a live card per port, KPIs, live
  Kaplan–Meier curves and Weibull fits per group, hub bandwidth, events, a drawer per drive,
  a setup wizard (host checks, self-test, guided enrollment that formats each stick) and
  the post-test protocol.
* **Data**: append-only CSV/JSON Lines per drive (`data/<campaign>/drives/<ID>/`), a life
  table and an Excel workbook written at the stop, and a ZIP export.

The rig uses the Python standard library only. On the test PC it runs on the official
CPython *embeddable* distribution that ships inside the package, so nothing is installed.

## Layout

```text
config/campaign.toml     design of run 2 (groups, plan of hubs and ports, timers)
src/flashrel/
  config.py              TOML -> validated campaign; scaled copy for the simulator
  payload.py, verify.py  reproducible test files with self-describing chunks; byte-exact check
  cycle.py               one copy-verify-delete cycle (retries, stop checks, dose counters)
  sentinel.py, exfat.py  data at rest and raw exFAT records: baseline at intake, periodic checks
  failure.py             F1-F6 / D1-D3, drive state, first failure and end of life
  worker.py              one process per drive: cycles, recovery protocol, censoring, live status
  controller.py          supervisor: workers, watchdog, presence, hub events, jobs, snapshot
  server.py, web/        local dashboard (vanilla HTML/CSS/JS, no internet needed)
  intake.py              safe enrollment (format, identity, sentinel, baseline, USB location)
  posttest.py            self-test, post-test check, re-check, port check, retire
  hostops.py             Windows host checks and (undoable) fixes
  analysis.py            Kaplan-Meier, Weibull MLE with censoring, Weibayes (pure Python)
  export.py, xlsx.py     life table, Excel workbook, ZIP
  platform/              windows (ctypes + PowerShell), posix (development), sim (simulated drives)
launcher/                Start/Stop .bat files and README_先读我.txt of the package
tools/                   make_assignment.py (randomized plan), build_package.py (Windows package)
tests/                   39 tests: exFAT on run-1 dumps, engine faults, worker and controller end to end
data/run1_fall2026/      the raw records of run 1 (for the pooled analysis)
```

## Development

```bash
uv run --python 3.14 --with pytest python -m pytest            # all tests (about 2 min)
uv run --python 3.14 --with pytest python -m pytest -m "not slow"
PYTHONPATH=src python3 -m flashrel app --sim --data /tmp/frsim   # demo: 22 simulated drives
```

The simulator (`--sim`, `--sim-speed`, `--sim-hours`) runs the real controller, workers and
dashboard on folders that stand in for drives, with made-up speeds and injected faults
(metadata loss, missing files, corruption, read-only, disconnects, I/O errors). Its numbers
are not measurements.

## Build the Windows package

```bash
python3 tools/build_package.py          # downloads python-3.14.10-embed-{amd64,arm64}.zip once
```

Output: `dist/FlashRel-Rig/` and `dist/FlashRel-Rig-v2.1.1.zip`. Copy the folder to
`C:\FlashRel-Rig` on the test PC and double-click `Start FlashRel.bat`.
