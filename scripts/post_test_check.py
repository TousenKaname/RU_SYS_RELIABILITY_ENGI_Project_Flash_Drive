"""Post-test check of every drive after the stop (the project's post-test analysis).

    python scripts/post_test_check.py configs/campaign.yaml --host W

For each drive of the campaign it reads back the identity file written at intake,
compares the volume's capacity and file system with the intake record, checks the
read-only flag, and runs the 64 MiB write, verify and delete check of the recovery
protocol with fresh data. The results go to ``posttest-<host>.json`` in the run folder
and, as a note, to each drive's event log. Drive states are not changed: a drive in the
attention state still goes through ``flashrel recheck`` and ``flashrel retire``.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from flashrel.config import load_campaign
from flashrel.failure import Event, EventType
from flashrel.payload import PAYLOAD_DIR
from flashrel.recorder import DriveBusy, DriveLog, drive_lock
from flashrel.runner import check_drive, check_summary
from flashrel.system.volumes import filesystem_name, find_drive, is_read_only, read_identity, usage


def check_one(campaign, drive_id: str, host: str) -> dict:
    log = DriveLog(campaign.run_dir, drive_id)
    intake = json.loads(log.intake_path.read_text()) if log.intake_path.exists() else {}
    state = log.load_state()
    port = state.port or campaign.assignments[drive_id].port
    record = {"drive_id": drive_id, "port": port, "status": state.status.value,
              "cycles_done": state.cycles_done,
              "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    mount = find_drive(drive_id)
    if mount is None:
        record["found"] = False
        return record
    ident = read_identity(mount) or {}
    volume = usage(mount)
    leftovers = sorted(p.name for p in (mount / PAYLOAD_DIR).iterdir()) \
        if (mount / PAYLOAD_DIR).exists() else []
    record.update(
        found=True, mount=str(mount),
        identity_intact=ident.get("drive_id") == drive_id
        and ident.get("enrolled_at") == intake.get("identity", {}).get("enrolled_at"),
        filesystem=filesystem_name(mount), total_bytes=volume.total, free_bytes=volume.free,
        capacity_unchanged=volume.total == intake.get("total_bytes"),
        read_only=is_read_only(mount), leftover_cycle_dirs=leftovers,
    )
    result = check_drive(mount, campaign, drive_id, host=host, port=port)
    record["check"] = check_summary(result)
    record["passed"] = bool(record["identity_intact"] and record["capacity_unchanged"]
                            and not record["read_only"] and result.outcome.value == "pass")
    log.append_event(Event(drive_id, state.cycles_done, EventType.NOTE, "", {
        "text": "post-test check", "passed": record["passed"],
        "identity_intact": record["identity_intact"],
        "capacity_unchanged": record["capacity_unchanged"], "read_only": record["read_only"],
        "check_outcome": result.outcome.value, "check_write_mbps": record["check"]["write_mbps"],
        "check_verify_mbps": record["check"]["verify_mbps"]}))
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config")
    ap.add_argument("--host", required=True)
    args = ap.parse_args(argv)
    campaign = load_campaign(args.config)
    results = []
    for drive_id, a in campaign.assignments.items():
        if a.host != args.host:
            continue
        try:
            with drive_lock(campaign.name, drive_id):
                record = check_one(campaign, drive_id, args.host)
        except DriveBusy:
            record = {"drive_id": drive_id, "skipped": "a worker is still testing this drive"}
        results.append(record)
        verdict = "skipped" if "skipped" in record else (
            "not found" if not record.get("found") else
            ("PASS" if record["passed"] else "FAIL"))
        print(f"{drive_id:8} {verdict:9} {json.dumps(record.get('check', {}))}")
    path = Path(campaign.run_dir) / f"posttest-{args.host}.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    return 0 if all(r.get("passed") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
