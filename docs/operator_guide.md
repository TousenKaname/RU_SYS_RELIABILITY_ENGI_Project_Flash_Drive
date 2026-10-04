# Operator guide

Day-to-day procedures for the flash-drive endurance test, 5–18 October 2026. Commands
assume the repository root as the working directory and `configs/campaign.yaml` as the
campaign. The test runs unattended; the hands-on work is the setup on 5 October, a
two-minute check once a day, and the analysis on 18 October.

| When | What | Hands-on |
| --- | --- | --- |
| 5 Oct | prepare hosts, intake of 9 drives, self-test, drill, start the pilot | ~1 h |
| 6 Oct, 09:00 | check the pilot, start the other 7 drives | ~10 min |
| 7–17 Oct | `flashrel status` once a day; protocol for a drive that needs attention | ~2 min/day |
| 18 Oct, 09:00 | the test stops by itself; export and analyze | ~15 min |

## 1. Prepare each host (5 Oct)

| | Windows (host W) | macOS (host M) |
| --- | --- | --- |
| Sleep | Power plan: never sleep, never turn off disks | `flashrel run` blocks sleep; keep the lid open and the charger in |
| USB power saving | Power options → USB selective suspend: **Disabled** | — |
| Updates | Pause Windows Update until 19 Oct | Turn off automatic updates |
| Indexing | Indexing Options: exclude the test drives | `sudo mdutil -i off /Volumes/<drive>` for each drive |
| Antivirus | Defender: add each test drive letter as an exclusion | — |

Use the docks' power adapters if they have one. Keep at least 1 cm between drives.
Label the hub ports W1–W3, A1–A3 (dock A), B1–B3 (dock B); W4 (front panel of host W)
is the spare port. Run `flashrel selftest` on every host; all four checks must say `ok`.
On the MacBook the second check counts the block reads of the read-back; if it fails,
the read-back came from memory and that host must not run the test.

## 2. Intake (5 Oct, once per drive)

1. Write the unit ID (e.g. `A8-03`) on the drive with a paint marker.
2. Quick-format as **exFAT** with the default allocation unit; volume label = unit ID.
3. `flashrel enroll configs/campaign.yaml --drive A8-03 --mount E:\` (a few seconds;
   the first test cycle verifies 90 % of the capacity, so no separate screen is needed).
4. Optional: note the USB vendor/product ID from Device Manager (Windows) or System
   Information → USB (macOS) with
   `flashrel note configs/campaign.yaml --drive A8-03 "VID 0x.. PID 0x.."`.

## 3. Pilot (5 Oct evening → 6 Oct morning)

Drill with the **reference drive** (any spare drive that is not under test): run
`flashrel portcheck configs/campaign.yaml --port W4 --mount <REF>` and pull the drive
while it writes. Expected: outcome `disconnected`; after re-plugging, the same command
reports `port W4: OK`.

Then start the two pilot drives:

```bash
flashrel run configs/campaign.yaml --host W --drives S8-01,A8-01
```

Next morning, `flashrel status configs/campaign.yaml` must show both drives `active`
with cycles of all three workloads, and `flashrel export` / `flashrel analyze` must run
on the pilot logs. Then stop the pilot supervisor (Ctrl+C) and start everything:

```bash
flashrel run configs/campaign.yaml --host W     # on host W: six drives
flashrel run configs/campaign.yaml --host M     # on host M: three drives
```

The pilot drives continue where they stopped; their pilot cycles count.

## 4. Daily check (7–17 Oct, once a day, in turn)

1. `flashrel status configs/campaign.yaml` — every drive `active`, cycles increasing.
2. A drive in `attention`? Follow section 5.
3. Copy the log folder to the backup location.

Never unplug a drive during a cycle except as part of this protocol.

## 5. A drive needs attention

1. Move the drive to the spare port W4 and run
   `flashrel recheck configs/campaign.yaml --drive A8-03 --port W4`
   (add `--host W` when the drive came from host M, so host M's supervisor leaves it alone).
   `recheck` only works on a drive in the attention state and refuses while a worker
   holds the drive.
2. **Passes on W4**: plug the reference drive into the old port and run
   `flashrel portcheck configs/campaign.yaml --port A2 --mount <REF>`.
   - Reference fails → the port is faulty:
     `flashrel port-fault configs/campaign.yaml --drive A8-03 --cycle <k> --port A2 --reason "REF failed on A2"`;
     take A2 out of service and keep the drive on W4.
   - Reference passes → the drive's failure stands as a soft failure (already logged by
     `recheck`); return the drive to its port: `flashrel note ... --port A2 "back on A2"`.
   - Restart the worker: `flashrel run configs/campaign.yaml --host W --drives A8-03`.
3. **Fails on W4**: reformat (exFAT, same label), restore its identity file with
   `flashrel enroll configs/campaign.yaml --drive A8-03 --mount <mount>`
   (the original intake record is kept), and recheck once more.
   Still failing → `flashrel retire configs/campaign.yaml --drive A8-03 --code F2 --reason "..."`.
   Use the code of the failure the drive shows (F1–F4, F6).

## 6. Stop (18 Oct, 09:00)

The supervisors censor every running drive at the stop time and exit by themselves.
Then:

```bash
flashrel export configs/campaign.yaml --out data/fall2026.xlsx
flashrel analyze configs/campaign.yaml --out reports/fall2026
```

and archive the log folder. The figures and tables in `reports/fall2026` go straight
into the slides.
