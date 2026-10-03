"""Command-line interface: ``flashrel <command> ...`` (see ``flashrel --help``)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import tempfile
from pathlib import Path

from flashrel import __version__
from flashrel.config import load_campaign
from flashrel.failure import DriveStatus, Event, EventType
from flashrel.recorder import DriveLog
from flashrel.system.volumes import candidate_mounts, read_identity, wait_for_drive
from flashrel.units import format_bytes


def _campaign(args):
    return load_campaign(args.config)


# -- harness commands ---------------------------------------------------------------------
def cmd_discover(args) -> int:
    found = False
    for mount in candidate_mounts():
        ident = read_identity(mount)
        if ident:
            found = True
            print(f"{ident.get('drive_id', '?'):8} {mount}  (enrolled {ident.get('enrolled_at')}, "
                  f"campaign {ident.get('campaign', '?')})")
    if not found:
        print("no enrolled drives are mounted")
    return 0


def cmd_enroll(args) -> int:
    from flashrel.intake import EnrollmentError, enroll

    campaign = _campaign(args)
    try:
        record = enroll(Path(args.mount), campaign, args.drive,
                        capacity_test=not args.skip_capacity_test, force=args.force)
    except EnrollmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2))
    if record.get("verdict") not in (None, "pass"):
        print("capacity screening FAILED: keep this drive out of the campaign", file=sys.stderr)
        return 1
    return 0


def cmd_run(args) -> int:
    from flashrel.runner import Supervisor
    from flashrel.sensors import SerialTemperatureSource

    temperature = None
    if args.temperature_port:
        names = {}
        if args.probe_map:
            import yaml

            names = yaml.safe_load(Path(args.probe_map).read_text(encoding="utf-8")) or {}
        temperature = SerialTemperatureSource(args.temperature_port, names)
    drives = args.drives.split(",") if args.drives else None
    Supervisor(args.config, args.host, drives, temperature=temperature,
               status_period_s=args.status_every).run()
    return 0


def cmd_status(args) -> int:
    campaign = _campaign(args)
    print(f"{'drive':8} {'host':4} {'port':10} {'status':10} {'cycles':>6} {'written':>10} "
          f"{'soft':>4}  last outcome      write MB/s  read MB/s  updated")
    for drive_id, a in campaign.assignments.items():
        log = DriveLog(campaign.run_dir, drive_id)
        state = log.load_state()
        rows = log.read_cycles()
        last = rows[-1] if rows else {}
        print(f"{drive_id:8} {a.host:4} {state.port or a.port:10} {state.status.value:10} "
              f"{state.cycles_done:>6} {format_bytes(state.bytes_written):>10} "
              f"{len(state.soft_failures):>4}  {last.get('outcome', '-'):16} "
              f"{last.get('write_mbps', '-'):>10} {last.get('verify_mbps', '-'):>10}  "
              f"{state.updated_at or '-'}")
    return 0


def cmd_recheck(args) -> int:
    from flashrel.cycle import Outcome
    from flashrel.runner import check_drive, check_summary

    campaign = _campaign(args)
    log = DriveLog(campaign.run_dir, args.drive)
    state = log.load_state()
    mount = wait_for_drive(args.drive, timeout_s=args.wait)
    if mount is None:
        print(f"{args.drive} is not mounted; plug it in and try again", file=sys.stderr)
        return 2
    port = args.port or state.port or campaign.assignments[args.drive].port
    result = check_drive(mount, campaign, args.drive, host=args.host or state.host, port=port)
    summary = check_summary(result)
    print(json.dumps(summary, indent=2))
    if result.outcome is not Outcome.PASS:
        print("check FAILED: try another port; if it fails there too, run `flashrel retire`",
              file=sys.stderr)
        return 1
    moved = port != (state.port or campaign.assignments[args.drive].port)
    log.append_event(Event(args.drive, state.cycles_done, EventType.RECOVERED, "D2", {
        "recovered_by": "operator re-check", "port": port, "moved_port": moved,
        "check": summary}))
    state.status, state.port = DriveStatus.ACTIVE, port
    log.save_state(state)
    print(f"{args.drive} is active again on port {port}")
    if moved:
        print("the drive passed on a new port: test the old port with the reference drive "
              "(`flashrel portcheck`) and log `flashrel port-fault` if the reference fails there")
    return 0


def cmd_portcheck(args) -> int:
    from flashrel.cycle import Outcome
    from flashrel.runner import check_drive, check_summary

    campaign = _campaign(args)
    result = check_drive(Path(args.mount), campaign, "REF", port=args.port)
    print(json.dumps(check_summary(result), indent=2))
    ok = result.outcome is Outcome.PASS
    print(f"port {args.port}: {'OK' if ok else 'FAULTY (reference drive failed)'}")
    return 0 if ok else 1


def cmd_port_fault(args) -> int:
    campaign = _campaign(args)
    log = DriveLog(campaign.run_dir, args.drive)
    log.append_event(Event(args.drive, args.cycle, EventType.PORT_FAULT, "F5", {
        "failure_cycle": args.cycle, "port": args.port, "reason": args.reason}))
    print(f"cycle-{args.cycle} failure of {args.drive} re-attributed to port {args.port} (F5)")
    return 0


def cmd_retire(args) -> int:
    campaign = _campaign(args)
    log = DriveLog(campaign.run_dir, args.drive)
    state = log.load_state()
    log.append_event(Event(args.drive, state.cycles_done, EventType.HARD_FAILURE, args.code,
                           {"reason": args.reason, "retired_by": "operator"}))
    state.status = DriveStatus.FAILED
    log.save_state(state)
    print(f"{args.drive} retired at cycle {state.cycles_done} ({args.code})")
    return 0


def cmd_note(args) -> int:
    campaign = _campaign(args)
    log = DriveLog(campaign.run_dir, args.drive)
    state = log.load_state()
    detail = {"text": args.text}
    if args.port:
        detail["port"] = args.port
        state.port = args.port
        log.save_state(state)
    log.append_event(Event(args.drive, state.cycles_done, EventType.NOTE, "", detail))
    return 0


def cmd_selftest(args) -> int:
    """Run a clean cycle and a fault-injected cycle on a local folder."""
    from flashrel.config import CycleSpec, WorkloadSpec
    from flashrel.cycle import Outcome, run_cycle
    from flashrel.payload import plan_cycle
    from flashrel.system.directio import cache_bypass_supported
    from flashrel.units import KiB, MiB

    work = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="flashrel-selftest-"))
    work.mkdir(parents=True, exist_ok=True)
    spec = CycleSpec(retries=1, retry_backoff_s=0)
    workload = WorkloadSpec("selftest", 4 * KiB, 4 * MiB, 32)

    def planner(volume):
        return plan_cycle(seed=1, drive_id="SELFTEST", cycle=1, workload=workload,
                          budget_bytes=32 * MiB, cluster_size=volume.cluster_size,
                          chunk_size=spec.chunk_size)

    def flip(cycle_dir, plan):
        target = cycle_dir / plan.files[0].relpath
        raw = bytearray(target.read_bytes())
        raw[len(raw) // 3] ^= 0x01
        target.write_bytes(bytes(raw))

    clean = run_cycle(work, planner, spec, drive_id="SELFTEST", cycle=1, workload="selftest")
    faulty = run_cycle(work, planner, spec, drive_id="SELFTEST", cycle=1, workload="selftest",
                       fault_hook=flip)
    checks = {
        "cache bypass available": cache_bypass_supported(),
        "clean cycle passes": clean.outcome is Outcome.PASS,
        "injected bit flip detected": faulty.outcome is Outcome.CORRUPTION
        and faulty.bits_corrupt == 1,
    }
    print(f"self-test in {work}: write {clean.write.mbps:.1f} MB/s, "
          f"read {clean.verify.mbps:.1f} MB/s ({clean.planned_files} files)")
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


# -- analysis commands --------------------------------------------------------------------
def cmd_export(args) -> int:
    from flashrel.analysis.export import write_workbook

    path = write_workbook(_campaign(args), Path(args.out))
    print(f"wrote {path}")
    return 0


def cmd_analyze(args) -> int:
    from flashrel.analysis.report import analyze_campaign

    for name, path in analyze_campaign(_campaign(args), Path(args.out)).items():
        print(f"{name:16} {path}")
    return 0


def cmd_simulate(args) -> int:
    from flashrel.analysis.planning import cycles_in, rotation_hours
    from flashrel.analysis.simulate import REHEARSAL_TRUTH, simulate_campaign

    campaign = _campaign(args)
    target = dataclasses.replace(campaign, data_dir=Path(args.out).resolve())
    horizon = {}
    for code, group in campaign.inventory.groups.items():
        truth = REHEARSAL_TRUTH.get(code)
        if truth is None:
            continue
        speeds = {w: (truth.write_mbps[w], truth.read_mbps[w]) for w in campaign.rotation}
        horizon[code] = int(cycles_in(args.days, rotation_hours(group.capacity_gb, speeds)))
    simulate_campaign(target, horizon, seed=args.seed)
    print(f"simulated logs for {len(target.assignments)} drives in {target.run_dir}")
    print("these are rehearsal data from made-up parameters, not measurements")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flashrel", description=__doc__)
    p.add_argument("--version", action="version", version=f"flashrel {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, func, help_, config=True):
        sp = sub.add_parser(name, help=help_, description=help_)
        if config:
            sp.add_argument("config", help="campaign YAML file, e.g. configs/phase1.yaml")
        sp.set_defaults(func=func)
        return sp

    add("discover", cmd_discover, "list mounted drives that carry an identity file", False)
    sp = add("enroll", cmd_enroll, "label a drive, screen its capacity, measure its baseline")
    sp.add_argument("--drive", required=True)
    sp.add_argument("--mount", required=True, help="drive letter or mount point, e.g. E:\\")
    sp.add_argument("--skip-capacity-test", action="store_true")
    sp.add_argument("--force", action="store_true", help="enroll even if the volume is not empty")
    sp = add("run", cmd_run, "cycle every drive assigned to this host until stopped")
    sp.add_argument("--host", required=True, help="host name used in the campaign, e.g. W")
    sp.add_argument("--drives", help="comma-separated subset of the host's drives")
    sp.add_argument("--temperature-port", help="serial port of the Arduino, e.g. COM5")
    sp.add_argument("--probe-map", help="YAML mapping probe ROM codes to names")
    sp.add_argument("--status-every", type=float, default=60.0, help="seconds between tables")
    add("status", cmd_status, "show every drive's progress")
    sp = add("recheck", cmd_recheck, "re-check a drive that needs attention")
    sp.add_argument("--drive", required=True)
    sp.add_argument("--port", help="port the drive is plugged into now")
    sp.add_argument("--host", help="host label, if it changed")
    sp.add_argument("--wait", type=float, default=30.0)
    sp = add("portcheck", cmd_portcheck, "test a port with the reference drive")
    sp.add_argument("--port", required=True)
    sp.add_argument("--mount", required=True, help="mount point of the reference drive")
    sp = add("port-fault", cmd_port_fault, "re-attribute a cycle failure to a faulty port (F5)")
    sp.add_argument("--drive", required=True)
    sp.add_argument("--cycle", type=int, required=True)
    sp.add_argument("--port", required=True)
    sp.add_argument("--reason", required=True)
    sp = add("retire", cmd_retire, "record a hard failure and stop testing a drive")
    sp.add_argument("--drive", required=True)
    sp.add_argument("--code", required=True, choices=["F1", "F2", "F3", "F4", "F6"])
    sp.add_argument("--reason", required=True)
    sp = add("note", cmd_note, "log an operator note (and optionally a port change)")
    sp.add_argument("--drive", required=True)
    sp.add_argument("--port")
    sp.add_argument("text")
    sp = add("selftest", cmd_selftest, "check the harness on this computer", False)
    sp.add_argument("--dir", help="folder to test in (default: a new temp folder)")
    sp = add("export", cmd_export, "write the campaign Excel workbook")
    sp.add_argument("--out", required=True)
    sp = add("analyze", cmd_analyze, "life table, Weibull fits, MCF, degradation, figures")
    sp.add_argument("--out", required=True)
    sp = add("simulate", cmd_simulate, "write synthetic rehearsal logs for a campaign")
    sp.add_argument("--out", required=True, help="data folder for the synthetic logs")
    sp.add_argument("--days", type=float, default=50.0)
    sp.add_argument("--seed", type=int, default=1)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
