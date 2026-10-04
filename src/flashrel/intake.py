"""Drive intake: enrollment and an optional capacity (counterfeit) screen.

Before a drive joins a campaign it is given an identity file that names its
unit ID (also written on its label). The first test cycle then fills 90 % of
the free space and verifies every byte, so a counterfeit drive that wraps
writes around its real capacity already shows up there as chunks that belong
to other files (``misdirected``).

The optional capacity screen (``capacity_test=True``) does the same check
before the test, over 99 % of the free space, and records the baseline
sequential throughput as cycle 0 (not counted as a test cycle). It costs one
full write and read of the drive.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from flashrel.config import Campaign, CycleSpec, WorkloadSpec
from flashrel.cycle import Outcome, run_cycle
from flashrel.payload import PAYLOAD_DIR, fill_budget, plan_cycle
from flashrel.recorder import DriveLog
from flashrel.system.volumes import (
    IDENTITY_FILE,
    VolumeUsage,
    filesystem_name,
    read_identity,
    usage,
    write_identity,
)
from flashrel.units import GB, GiB, MiB

#: Entries an operating system may create on a freshly formatted volume.
SYSTEM_ENTRIES = {
    "System Volume Information", "$RECYCLE.BIN", ".Spotlight-V100", ".fseventsd", ".Trashes",
    "._.Trashes", ".TemporaryItems", ".DS_Store", IDENTITY_FILE, PAYLOAD_DIR,
}
CAPACITY_WORKLOAD = WorkloadSpec("capacity", min_size=256 * MiB, max_size=1 * GiB,
                                 files_per_dir=16)
CAPACITY_FILL = 0.99


class EnrollmentError(RuntimeError):
    pass


def enroll(mount: Path, campaign: Campaign, drive_id: str, *, capacity_test: bool = False,
           force: bool = False, stop: threading.Event | None = None) -> dict[str, Any]:
    """Enroll the volume at ``mount`` as drive ``drive_id``, optionally screening it."""
    unit = campaign.inventory[drive_id]
    mount = Path(mount)
    current = read_identity(mount)
    if current and current.get("drive_id") != drive_id and not force:
        raise EnrollmentError(f"{mount} is already enrolled as {current.get('drive_id')}")
    foreign = sorted(p.name for p in mount.iterdir() if p.name not in SYSTEM_ENTRIES)
    if foreign and not force:
        raise EnrollmentError(f"{mount} is not empty ({', '.join(foreign[:5])}); "
                              "format it as exFAT first or pass --force")
    volume = usage(mount)
    fs = filesystem_name(mount)
    identity = write_identity(mount, drive_id, campaign=campaign.name, total_bytes=volume.total,
                              filesystem=fs)
    record: dict[str, Any] = {
        "drive_id": drive_id,
        "group": unit.group.code,
        "identity": identity,
        "filesystem": fs,
        "total_bytes": volume.total,
        "free_bytes": volume.free,
        "cluster_size": volume.cluster_size,
        "nominal_gb": unit.capacity_gb,
        "usable_fraction_of_nominal": round(volume.total / (unit.capacity_gb * GB), 4),
    }
    if capacity_test:
        record["capacity_test"] = capacity_screen(mount, campaign.seed, drive_id, campaign.cycle,
                                                  stop=stop)
        record["verdict"] = record["capacity_test"]["outcome"]
    DriveLog(campaign.run_dir, drive_id).save_intake(record)
    return record


def capacity_screen(mount: Path, seed: int, drive_id: str, spec: CycleSpec, *,
                    stop: threading.Event | None = None) -> dict[str, Any]:
    """Fill 99 % of the volume with large files, read them back, delete them."""
    def planner(volume: VolumeUsage):
        budget = fill_budget(volume.free, CAPACITY_FILL, spec.reserve_bytes)
        return plan_cycle(seed=seed, drive_id=drive_id, cycle=0, workload=CAPACITY_WORKLOAD,
                          budget_bytes=budget, cluster_size=volume.cluster_size,
                          chunk_size=spec.chunk_size)

    result = run_cycle(mount, planner, spec, drive_id=drive_id, cycle=0, workload="capacity",
                       stop=stop)
    return {
        "outcome": result.outcome.value,
        "suspected_fake_capacity": result.outcome is Outcome.CORRUPTION and
        result.corruption_kinds.get("misdirected", 0) + result.corruption_kinds.get("stale", 0) > 0,
        "bytes_written": result.write.bytes,
        "write_mbps": round(result.write.mbps, 3),
        "verify_mbps": round(result.verify.mbps, 3),
        "write_seconds": round(result.write.seconds, 1),
        "verify_seconds": round(result.verify.seconds, 1),
        "bad_files": len(result.bad_files),
        "corruption_kinds": dict(result.corruption_kinds),
        "errors": [e.detail for e in result.errors if not e.recovered][:5],
    }
