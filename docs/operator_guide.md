# Runbook: one-day test, 23–24 October 2026

The whole test runs on one Windows desktop (host W) with one USB-C dock. A team member
runs the commands below; between the start of the full test (step 5) and the stop, the
test runs unattended. Commands assume the repository root as the working
directory; `C` stands for `configs/campaign.yaml`.

| When | Step | Hands-on |
| --- | --- | --- |
| 23 Oct 09:00 | 1. plug in the eight drives; 2. prepare the PC | ~10 min |
| 23 Oct 09:15 | 3. format, enroll, self-test | ~30 min |
| 23 Oct 10:00 | 4. pilot on S8-01 and A8-01 (2 h) | ~5 min |
| 23 Oct 12:00 | 5. check the pilot, start all eight drives | ~10 min |
| 24 Oct 12:00 | the test stops by itself | — |
| 24 Oct 12:00 | 6. post-test check, export, analysis | ~1 h |

## 1. Plug in the drives

Write the unit ID on each drive and plug it into its port (`assignments` in
`configs/campaign.yaml`):

| Port | Drive | Port | Drive |
| --- | --- | --- | --- |
| W1 | S8-01 | W5 | A8-03 |
| W2 | A16-01 | W6 | A8-04 |
| W3 | A8-01 | D1 (dock) | S8-02 |
| W4 | A8-02 | D2 (dock) | A16-02 |

W1–W6 are the desktop's six USB-A ports, D1–D2 the two ports of the USB-C dock. A8-05
is the spare drive: keep it unplugged until step 6.

## 2. Prepare the PC (needs an administrator PowerShell)

```powershell
powercfg /change standby-timeout-ac 0          # never sleep (flashrel run also blocks sleep)
powercfg /change disk-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setactive SCHEME_CURRENT             # USB selective suspend off
```

Pause Windows Update for one week (Settings → Windows Update) and add each test drive
letter as a Defender exclusion (`Add-MpPreference -ExclusionPath E:\`).

## 3. Format, enroll, self-test

For each drive (look up its letter with `Get-Volume`), quick-format as exFAT with the
volume label equal to the unit ID, then enroll it:

```powershell
Format-Volume -DriveLetter E -FileSystem exFAT -NewFileSystemLabel A8-03 -Confirm:$false
flashrel enroll C --drive A8-03 --mount E:\
```

Then `flashrel selftest`; all four checks must say `ok`.

## 4. Pilot (10:00–12:00)

```bash
flashrel run C --host W --drives S8-01,A8-01
```

Run it in the background. At 12:00, `flashrel status C` must show both drives `active`
with a completed large-file and medium-file cycle and no software error, and
`flashrel export C --out data/pilot.xlsx` and `flashrel analyze C --out reports/pilot`
must run. Note the measured write/read speeds (in the Cycles sheet) and put them into
`scripts/planning_assumptions.py`. If any check fails, fix the problem and rerun the
pilot before step 5.

## 5. Start all eight drives (12:00)

Leave the pilot running and start the other six drives in a second supervisor (each
drive has its own lock, so the two never touch the same drive):

```bash
flashrel run C --host W --drives A16-01,A8-02,A8-03,A8-04,S8-02,A16-02
```

Both supervisors censor every running drive at the stop time (`stop.end_time`,
24 Oct 12:00) and exit by themselves. Do not unplug any
drive while it runs. To run longer, change `stop.end_time` and start `flashrel run`
again; the workers resume from their logs.

## 6. After the stop: post-test check, export, analysis

1. `flashrel status C` lists every drive that ended in `attention` (its automatic
   recovery check failed during the test). For each such drive:
   - move it to a port freed by a surviving drive (e.g. W1) and run
     `flashrel recheck C --drive A8-03 --port W1`;
   - **fails again** → `flashrel retire C --drive A8-03 --code F2 --reason "..."`, using
     the code of the failure it showed (F1–F4, F6): hard failure at its failure cycle;
   - **passes** → plug the spare drive A8-05 into the old port and run
     `flashrel portcheck C --port D1 --mount <A8-05 mount>`; if the spare fails there,
     `flashrel port-fault C --drive A8-03 --cycle <k> --port D1 --reason "spare failed on D1"`.
2. Export and analyze:

```bash
flashrel export C --out data/fall2026.xlsx
flashrel analyze C --out reports/fall2026
```

3. Copy the log folder (`data/`) to cloud storage.
