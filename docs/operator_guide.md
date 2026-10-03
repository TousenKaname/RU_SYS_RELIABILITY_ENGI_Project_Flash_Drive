# Operator guide

Day-to-day procedures for the flash-drive endurance test. Commands assume the
repository root as the working directory and `configs/phase1.yaml` as the campaign.

## 1. Prepare each host (once)

| | Windows (host W) | macOS (host M) |
| --- | --- | --- |
| Sleep | Power plan: never sleep, never turn off disks | `flashrel run` blocks sleep; also untick "Put hard disks to sleep" |
| USB power saving | Power options → USB selective suspend: **Disabled** | — |
| Updates | Pause Windows Update until 1 Dec | Turn off automatic updates |
| Indexing | Indexing Options: exclude the test drives | `sudo mdutil -i off /Volumes/<drive>` for each drive |
| Antivirus | Defender: add each test drive letter as an exclusion | — |
| Removal policy | Device Manager → drive → Policies: **Quick removal** (default) | — |

Use the docks' power adapters if they have one. Keep at least 1 cm between drives.
Label the hub ports W1–W3, A1–A3 (dock A), B1–B3 (dock B); W4 (front panel of host W)
is the spare port. Run `flashrel selftest` on every host; all three checks must say `ok`.

## 2. Intake (once per drive)

1. Write the unit ID (e.g. `A8-03`) on the drive with a paint marker.
2. Quick-format as **exFAT** with the default allocation unit; volume label = unit ID.
3. `flashrel enroll configs/phase1.yaml --drive A8-03 --mount E:\`
   (about one full write and read of the drive). The verdict must be `pass`; a drive
   that fails the capacity screen is replaced, not tested.
4. Note the USB vendor/product ID shown by Device Manager (Windows) or System
   Information → USB (macOS): `flashrel note configs/phase1.yaml --drive A8-03 "VID 0x.. PID 0x.."`.
5. Tape a DS18B20 probe on the housing and record the probe ROM code in `configs/probes.yaml`.

## 3. Pilot (6–11 Oct)

```bash
flashrel run configs/phase1.yaml --host W --drives S8-01,A8-01
```

Fault drills use the **reference drive** (a spare drive that is not under test):

| Drill | How | Expected |
| --- | --- | --- |
| bit flip | `flashrel selftest --dir <REF mount>` | `injected bit flip detected: ok` |
| unplug mid-write | `flashrel portcheck configs/phase1.yaml --port W4 --mount <REF>` and pull the drive during the run | outcome `disconnected` |
| resume | Ctrl+C the supervisor during a cycle, start it again | worker continues at the same cycle number |
| port check | `flashrel portcheck ... --port A1 --mount <REF>` | `port A1: OK` |

Then `flashrel export` and `flashrel analyze` on the pilot logs, and update
`scripts/planning_assumptions.py` with the measured speeds.

## 4. Daily check (morning and evening, rotating)

1. `flashrel status configs/phase1.yaml` — every drive `active`, cycles increasing.
2. Any drive in `attention`? Follow section 5 within 12 hours.
3. `flashrel analyze configs/phase1.yaml --out reports/daily` and open
   `common_cause.csv`: failures of two drives on one dock within 10 min go through the
   port check (step 5.2) before they count.
4. Copy the log folder to the backup location.
5. Log anything unusual: `flashrel note configs/phase1.yaml --drive <id> "text"`.

Never unplug a drive during a cycle except as part of this protocol.

## 5. A drive needs attention

1. Move the drive to the spare port W4 and run
   `flashrel recheck configs/phase1.yaml --drive A8-03 --port W4`.
2. **Passes on W4**: plug the reference drive into the old port and run
   `flashrel portcheck configs/phase1.yaml --port A2 --mount <REF>`.
   - Reference fails → the port is faulty:
     `flashrel port-fault configs/phase1.yaml --drive A8-03 --cycle <k> --port A2 --reason "REF failed on A2"`;
     take A2 out of service and keep the drive on W4.
   - Reference passes → the drive's failure stands as a soft failure (already logged by
     `recheck`); return the drive to its port: `flashrel note ... --port A2 "back on A2"`.
   - Restart the worker: `flashrel run configs/phase1.yaml --host W --drives A8-03`
     (or restart the host's supervisor).
3. **Fails on W4**: reformat (exFAT, same label), restore its identity file with
   `flashrel enroll configs/phase1.yaml --drive A8-03 --mount <mount> --skip-capacity-test`
   (the original intake record is kept), and recheck once more.
   Still failing → `flashrel retire configs/phase1.yaml --drive A8-03 --code F2 --reason "..."`.
   Use the code of the failure the drive shows (F1–F4, F6).

## 6. Stop (1 Dec, 09:00)

The supervisors censor every running drive at the stop time and exit. Then run
`flashrel export` and `flashrel analyze`, and archive the log folder.
