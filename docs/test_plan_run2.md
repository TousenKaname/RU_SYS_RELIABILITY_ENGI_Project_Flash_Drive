# Test plan, run 2: 20 USB flash drives, two 10-port hubs, 120 hours

ISE 16:540:585 Systems Reliability Engineering I, Project 4, Group 4 (Guoan Wang,
Shangqing Wei, Mengtong Ai). Campaign `fall2026b`, software FlashRel Rig 2.1.

## 1. Why a second run

Run 1 (`fall2026`, 23–24 Oct 2026) tested eight drives for 24 hours: five of the six
ABLAZE drives failed, both SanDisk drives survived. At the proposal talk the instructor
judged eight drives too few and asked for a test in which a large share of the drives
fails. Run 1 also left four open points that this plan closes:

1. Too few drives per group (two to four) for a life distribution per group.
2. Unequal host conditions: the one ABLAZE 8 GB survivor sat in a port that Windows
   treats as internal ("expect no removal"), so its file-system writes were cached.
3. Failures were found late: the A8 drives lost their file-system records during an
   11–18 h delete phase; one loss was found by hand, the others only at the end of the phase.
4. Gaps in the software: no stop-time check in the delete phase; the recovery check
   deleted the damaged folder first and therefore failed for a reason unrelated to the drive.

## 2. Research questions

* **RQ1 capacity.** Within ABLAZE, do 16 GB and 8 GB drives differ in cycles to failure?
* **RQ2 manufacturer.** At 8 GB, do SanDisk and ABLAZE drives differ? (two SanDisk drives
  only; they continue from run 1, so their run-1 cycles enter as delayed entry)
* **RQ3 file size.** Does the file size of a cycle change the probability that the cycle fails?
* **RQ4 mechanism and warning signs.** Is life governed by data volume (full-drive writes)
  or by file-system operations (the metadata hot spot seen in run 1)? Which indicators
  precede a failure?

## 3. Test articles

| Group | Drive | Units | Role |
| --- | --- | --- | --- |
| A16 | ABLAZE 16 GB metal USB 2.0 (10-pack, Amazon B0CLR9B4MV) | A16-03 … A16-12 | 10 tested |
| A8 | ABLAZE 8 GB metal USB 2.0 (10-pack, Amazon B0CLR6CBKG) | A8-06 … A8-15 | 8 tested, 2 spares (A8-09, A8-15) |
| S8 | SanDisk Cruzer Blade 8 GB (from run 1) | S8-01, S8-02 | 2 tested; prior use 26 and 23 cycles (174.1 and 153.1 GB written) |

Unit numbers continue run 1, so every ID is unique across both runs. The reused SanDisk
drives are identified by their run-1 USB serial numbers at intake. The spares are drawn at
random (seed 20261116) and serve the self-test before the start and the port checks after it.

## 4. Rig

* One Windows 10/11 PC, run as administrator, sleep off, Windows Update paused.
* Two Sabrent HB-BU10 hubs (10 USB 3.0 ports, 12 V / 5 A adapter, a switch per port),
  each plugged directly into its own rear USB port of the PC. Every drive under test sits
  on a hub port; none on the PC's own ports, so all 20 drives share one kind of
  connection path and the same Windows removal policy (recorded per drive and checked
  before the start).
* All drives are USB 2.0 devices, so each hub forwards them over one 480 Mbit/s link
  (about 35–40 MB/s in practice). Large-file phases are partly limited by this shared
  link; the controller logs the load of each hub every minute (`hub_load.csv`) so that
  throughput can be read against it.
* **Blocks and randomization.** Each hub carries 5 A16 + 4 A8 + 1 S8; the order of the
  ten drives over the ten ports of a hub is random (the ports of a 10-port hub sit behind
  different internal hub chips). Hub is therefore a block, not confounded with group.

Plan (seed 20261116, `tools/make_assignment.py`):

| Port | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Hub A | A16-10 | A16-05 | A16-12 | A8-06 | S8-01 | A8-08 | A8-10 | A8-14 | A16-04 | A16-06 |
| Hub B | A16-09 | A16-11 | A8-11 | A8-07 | A8-13 | S8-02 | A16-07 | A8-12 | A16-03 | A16-08 |

## 5. Intake

For every drive, one at a time: safety checks (USB bus, not the system disk, size within
±15 % of the group's nominal capacity, run-1 serial for S8), quick format to exFAT with
32 KiB clusters and the unit ID as label, then two files that are never changed again:
the identity file (`FLASHREL-ID.json`, with a random token) and a 1 MiB sentinel file of
reproducible data written right after it, next to the file-system records at the start of
the volume. Both are read back around the cache, and a raw baseline of the exFAT records
is taken (boot-region checksum, up-case table checksum, root-directory entry sets); a raw
check that does not pass on the fresh volume is switched off for that drive, so it can
never produce a false failure. The USB serial number, the USB location path (PC port and
port chain inside the hub) and the Windows removal policy are recorded.

## 6. The cycle (unchanged from run 1)

Copy → verify → delete. Each cycle fills 90 % of the free space (never the last 64 MiB)
with files of one size class, in the fixed rotation large → medium → small:

| Workload | File sizes (log-uniform) | Files per cycle, 8 GB / 16 GB |
| --- | --- | --- |
| large | 512 MiB – 2 GiB | about 6 / 12 |
| medium | 1 – 64 MiB | about 450 / 900 |
| small | 4 – 256 KiB | about 90,000 / 176,000 |

Files are generated in memory from 64-bit keys (no source files on the host), flushed
file by file, read back with `FILE_FLAG_NO_BUFFERING` and compared byte for byte; every
1 MiB chunk carries a header that classifies a bad chunk (bit errors, stale, misdirected,
erased, truncated). Errors are retried twice (5 s, 10 s).

## 7. What run 2 adds

1. **Checks of data at rest and file-system records** every 10 minutes during every
   phase, at every phase boundary and at the start of every cycle: identity and
   sentinel files read around the cache; exFAT boot-region checksum, up-case table
   checksum and root-directory entry sets read raw. A finding must persist over three
   reads 5 s apart. A confirmed finding ends the cycle at once as F1 (data at rest), F6
   (damaged file-system records) or F2 (a record that cannot be read), so a failure is
   time-stamped within ten minutes. Every record a finding covers goes on record at once.
   A confirmed finding is the cycle's outcome even if a stop, a disconnect or an I/O
   error ends the cycle afterwards.
2. **Dose counters.** Bytes written and read, files created and deleted, directories and
   flushes are counted for every drive, including the partial cycles cut short by a stop
   or a crash; every failure carries the exact counts at its detection. Life is recorded
   in four time scales: cycles, hours on test, full-drive writes (FDW), file operations.
3. **Recovery protocol** after a cycle failure: wait for the drive (2 min) → restart the
   USB device in software (`pnputil /restart-device`, the equivalent of re-plugging it) →
   no readable file system (RAW)? (attention) → read-only? capacity changed? (end of life)
   → sentinel check → write, verify and delete 64 MiB of fresh data in a separate folder,
   *without* first deleting the failed cycle's folder. A drive that passes keeps cycling
   (intermittent failure, D2) and its failed folder is deleted (retried like any delete;
   quarantined only if it still cannot be deleted); one that does not stops in the
   attention state until the post-test protocol, with the failed folder left on it as
   evidence. Damage already on record is not counted again.
4. **Stop time** checked between chunks and files in every phase, including delete.
5. **Hub events.** When three or more drives of one hub vanish within 30 s of each other
   (each worker records the moment its drive vanished; the controller's presence scan
   adds what it saw; the count is taken after a 12 s settle time), the event is a hub
   fault (F5): not counted against the drives, which wait for the hub to return. A drive
   that is not back 2 min after the hub's other drives are back has failed on its own (F3).
6. **Hang watchdog** in every phase (no progress for 10 min → F3 with the wear and hours
   of the hung cycle, then the recovery protocol; a hang in the recovery protocol →
   attention, with no new cycle booked; a first hang inside a scan of every volume, which
   another stick can hold up, only restarts the worker), worker restart after a crash,
   resume after a power cut or restart (the cycle in progress is logged as interrupted
   with its wear, not counted; a failed cycle whose state was not saved is put back from
   its row in cycles.csv).
7. **Post-test data retention**: the files of the cut-short or failed cycle that are still
   on a drive are re-read and compared after the test.

## 8. Failure definitions

| Code | Failure mode | Code | Degradation indicator |
| --- | --- | --- | --- |
| F1 | data corruption: read-back differs, files missing (also a file gone between its read-back and its delete), or a file at rest damaged | D1 | write throughput < 50 % of the drive's baseline for 3 consecutive cycles of a workload |
| F2 | unrecoverable I/O error | D2 | intermittent failure: a cycle failure after which the drive passes the recovery check |
| F3 | disconnect, not found, or hang (10 min without progress) | D3 | transient error cleared by a retry or a second read |
| F4 | read-only lock | | |
| F5 | port, hub or host fault (not counted) | | |
| F6 | file-system fault: records damaged, the file system no longer mounts (RAW, stick still on the bus), space not returned, capacity change | | |

* **Time to failure T** (primary): the cycle of the first cycle failure (F1–F4, F6),
  with the dose and hours at its detection.
* **End of life** (secondary): the drive fails the recovery protocol and its re-check on
  another port, locks read-only, changes capacity, or has 3 cycle failures within 10 cycles.
* **Censoring** (Type I): drives still working 120 h after the start are censored at the
  cycles they completed; the cycle in progress at the stop is not counted.

## 9. Schedule

| Step | Duration |
| --- | --- |
| Host checks, connect the hubs, label drives and ports | 30 min |
| Enroll the spares, self-test (cache bypass, clean cycle, injected 1-bit corruption) | 15 min |
| Enroll the 20 drives one by one | 20–30 min |
| Test (workers start 15 s apart, alternating hubs) | 120 h |
| Post-test protocol, export | 1–2 h |

## 10. Expected course (from run 1's measured speeds)

| Group | large / medium cycle | small cycle | run-1 behaviour |
| --- | --- | --- | --- |
| A8 | 0.3–0.6 h each | 11–20 h (slow deletes, about 3 files/s) | 3 of 4 failed in their first small cycle (cycle 3) |
| A16 | 0.9–1.8 h each | about 5 h | 2 of 2 failed in their 2nd and 3rd small cycle |
| S8 | 0.6–1.2 h each | about 1.8 h | 0 of 2 failed in 23–26 cycles |

The ranges for large and medium cycles allow for the shared hub link. If the new lots
behave like run 1's, most of the 18 ABLAZE drives fail within the first one to two days,
and the SanDisk drives reach about 100 further full-drive writes by the stop (the level at
which earlier studies saw the first wear-out failures).

## 11. Analysis plan (after the test)

* Life table per drive; Kaplan–Meier per group with Greenwood bounds.
* Weibull and lognormal MLE with right censoring per group; likelihood-ratio test of a
  capacity effect (A8 vs A16) and of a common shape; MTTF, B10, 90 % intervals.
* SanDisk: delayed entry at its run-1 age; Weibayes lower bound if fewer than two failures.
* File size: failure probability per cycle by workload with exact binomial bounds;
  discrete-time hazard model (complementary log-log) with workload, group and hub.
* Time scale comparison: which of cycles, FDW and file operations makes the lives of the
  two capacities most alike (tests the metadata hot-spot explanation of run 1).
* Hub block: log-rank test hub A vs hub B.
* Degradation: throughput and per-file latency paths, delete rates within long phases
  (`trace.csv`), D1/D2/D3 counts, mean cumulative function of repeated failures.
* Pooled analysis with run 1 (lot as a factor): A8 n = 12, A16 n = 12.
* Temperature (manual readings, PC thermal zone) as a descriptive covariate.

## 12. Threats to validity

One PC and one purchase lot per group; the brand comparison rests on two reused drives;
the hub link is shared, so throughput depends on the activity of neighbouring drives
(logged); no temperature control; a host cycle is not a NAND program/erase cycle (hence
the four time scales); the drives cycle without rest, so the results describe continuous
heavy use.
