"""Self-test before the start, and the post-test protocol after the stop.

Self-test (on a spare drive, before the 20 drives start): the cache bypass works, the
drive's files and file-system records pass their checks, a clean 64 MiB cycle passes,
and an injected one-byte corruption is caught.

Post-test protocol (project description 4.4 "Post-Test Analysis"):

* **post-test check** of every drive on its test port: identity, sentinel, file-system
  records, the files of the cut-short or failed cycle that are still on the drive
  (data retention), and a fresh 64 MiB write-verify-delete check;
* **re-check on another port** for every drive that stopped in the attention state;
* **port check**: a spare drive on the port of a failed drive. If the spare fails there
  too, the failure is the port's (F5) and is not counted against the drive;
* **retire**: the operator's final classification of each drive that stopped.

Nothing is ever repaired or formatted after the start.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from flashrel.config import Campaign
from flashrel.cycle import CycleResult, CycleRunner, Hooks
from flashrel.failure import DriveState, Status
from flashrel.payload import CHECK_DIR, PAYLOAD_DIR, fill_budget, plan_cycle
from flashrel.platform import Platform, Unsupported, Volume
from flashrel.sentinel import SentinelChecker, locate
from flashrel.store import CampaignStore, FileLock, atomic_write_json, read_json
from flashrel.timeutil import iso
from flashrel.units import MiB
from flashrel.verify import FileStatus, verify_file
from flashrel.worker import CHECK_WORKLOAD


class ActionError(RuntimeError):
    pass


def _intake(cstore: CampaignStore, drive_id: str) -> dict[str, Any]:
    intake = cstore.drive(drive_id).load_intake()
    if not intake:
        raise ActionError(f"{drive_id} is not enrolled")
    return intake


def _locate(platform: Platform, drive_id: str, intake: dict[str, Any]) -> tuple[Volume, str]:
    vol, how = locate(platform, drive_id, intake)
    if vol is None:
        raise ActionError(f"{drive_id} is not plugged in (or cannot be identified)")
    return vol, how


def location_of(platform: Platform, vol: Volume) -> dict[str, Any]:
    """Current USB location of a stick (hub key and port chain), from the inventory."""
    if not platform.supports_inventory:
        return {}
    from flashrel.intake import match_disk

    info = platform.storage_info(vol.root)
    if info is None:
        return {}
    try:
        disk = match_disk(platform.inventory(), info.disk_number, info.serial)
    except Exception as exc:  # PowerShell unavailable
        return {"error": str(exc)[:200]}
    if not disk:
        return {}
    return {k: disk.get(k) for k in ("hub_key", "port_chain", "location_info", "location_paths",
                                     "removal_policy")}


def fresh_check(campaign: Campaign, platform: Platform, vol: Volume, drive_id: str, *,
                after_write=None) -> CycleResult:
    """64 MiB of new data in FLASHREL-CHECK: written, read back around the cache, deleted.
    The leftovers of earlier cycles are not touched."""
    key = f"{drive_id}:check:{uuid.uuid4().hex}"

    def planner(free: int, cluster: int):
        budget = min(fill_budget(free, campaign.cycle.fill_fraction, campaign.cycle.reserve_bytes),
                     campaign.failure.check_bytes)
        return plan_cycle(seed=campaign.seed, drive_id=drive_id, cycle=0, workload=CHECK_WORKLOAD,
                          budget_bytes=budget, cluster_size=cluster, chunk_size=campaign.cycle.chunk_size,
                          key_space=key, root_dir=CHECK_DIR)

    runner = CycleRunner(platform, vol.root, planner, campaign.cycle, drive_id=drive_id, cycle=0,
                         workload="check", hooks=Hooks(after_write=after_write), data_dir=CHECK_DIR,
                         clear_dirs=(CHECK_DIR,))
    return runner.run()


def _plain(r: CycleResult) -> str:
    text = (f"{r.write.files} files, {r.write.bytes / 1e6:.1f} MB written at {r.write.mbps:.2f} MB/s, "
            f"read back at {r.verify.mbps:.2f} MB/s")
    if r.files_corrupt or r.files_missing:
        text += f"; {r.files_corrupt} corrupt file(s), {r.bits_corrupt} wrong bit(s), {r.files_missing} missing"
    errors = [e.detail for e in r.errors if not e.recovered]
    return text + (f"; error: {errors[0]}" if errors else "")


def _summary(r: CycleResult) -> dict[str, Any]:
    return {"outcome": r.outcome.value, "code": r.code, "files": r.write.files, "bytes": r.write.bytes,
            "write_mbps": round(r.write.mbps, 3), "read_mbps": round(r.verify.mbps, 3),
            "files_corrupt": r.files_corrupt, "files_missing": r.files_missing,
            "errors": [e.detail for e in r.errors if not e.recovered][:5]}


def _with_lock(cstore: CampaignStore, drive_id: str):
    lock = FileLock(cstore.drive(drive_id).lock_path)
    if not lock.acquire():
        raise ActionError(f"{drive_id} is busy (its worker is running)")
    return lock


def _record(cstore: CampaignStore, section: str, key: str, value: dict[str, Any]) -> None:
    path = cstore.host_dir / "posttest.json"
    data = read_json(path, {}) or {}
    data.setdefault(section, {})[key] = value
    data["updated_at"] = iso()
    atomic_write_json(path, data)


# -- self-test ------------------------------------------------------------------------------------
def selftest(campaign: Campaign, cstore: CampaignStore, platform: Platform, drive_id: str,
             progress=lambda text: None) -> dict[str, Any]:
    intake = _intake(cstore, drive_id)
    lock = _with_lock(cstore, drive_id)
    checks: list[dict[str, Any]] = []
    try:
        vol, how = _locate(platform, drive_id, intake)
        checks.append({"id": "bypass", "title": "Read-back bypasses the host cache",
                       "ok": platform.cache_bypass(),
                       "detail": "FILE_FLAG_NO_BUFFERING" if platform.name == "windows" else platform.name})
        progress("checking identity, sentinel and file-system records")
        sres = SentinelChecker(platform, intake, campaign.sentinel).check(vol)
        checks.append({"id": "sentinel", "title": "Identity, sentinel and file-system records intact",
                       "ok": sres.ok, "detail": sres.detail or ", ".join(
                           f"{k} ok" for k, v in sres.components.items() if v.get("ok"))})
        raw_ok = bool((intake.get("exfat") or {}).get("ok"))
        checks.append({"id": "raw", "title": "Raw exFAT checks available", "ok": raw_ok,
                       "warn_only": True,
                       "detail": "boot region, up-case table, root directory" if raw_ok
                       else (intake.get("exfat") or {}).get("error", "")})
        progress("clean 64 MiB cycle")
        clean = fresh_check(campaign, platform, vol, drive_id)
        checks.append({"id": "clean", "title": "A clean 64 MiB cycle passes",
                       "ok": clean.outcome.value == "pass", "detail": _plain(clean), "data": _summary(clean)})

        def inject(cycle_dir: str, plan) -> None:
            first = plan.files[0]
            path = os.path.join(cycle_dir, *first.relpath.split("/"))
            with open(path, "r+b") as fh:
                fh.seek(min(first.size - 1, 4096))
                byte = fh.read(1)
                fh.seek(-1, os.SEEK_CUR)
                fh.write(bytes([byte[0] ^ 0x10]))
                fh.flush()
                os.fsync(fh.fileno())

        progress("cycle with one injected bit flip")
        faulty = fresh_check(campaign, platform, vol, drive_id, after_write=inject)
        caught = faulty.outcome.value == "corruption" and faulty.files_corrupt >= 1 and faulty.bits_corrupt == 1
        checks.append({"id": "inject", "title": "An injected one-bit corruption is detected",
                       "ok": caught, "detail": ("caught: " if caught else "NOT caught: ") + _plain(faulty),
                       "data": _summary(faulty)})
        loc = location_of(platform, vol)
        if platform.supports_inventory:
            checks.append({"id": "location", "title": "USB location and removal policy readable",
                           "ok": bool(loc.get("hub_key")), "warn_only": True,
                           "detail": f"{loc.get('hub_key', '?')} port {loc.get('port_chain', '?')}, "
                                     f"removal policy {loc.get('removal_policy')}"})
    finally:
        lock.release()
    passed = all(c["ok"] or c.get("warn_only") for c in checks)
    result = {"time": iso(), "drive": drive_id, "passed": passed, "checks": checks, "platform": platform.name}
    atomic_write_json(cstore.host_dir / "selftest.json", result)
    cstore.drive(drive_id).event("selftest", 0, passed=passed, checks=checks)
    cstore.operator("selftest", drive=drive_id, passed=passed)
    return result


# -- post-test ------------------------------------------------------------------------------------
def _leftover_cycle(cstore: CampaignStore, drive_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """The cycle whose files should still be on the drive: cut short at the stop, or failed."""
    events = cstore.drive(drive_id).read_events()
    cut = [e for e in events if e.get("type") == "interrupted" and e["detail"].get("free_before")]
    if cut and state.get("status") == Status.CENSORED.value:
        e = cut[-1]
        return {"cycle": e["cycle"], "workload": e["detail"].get("workload"),
                "free_before": e["detail"]["free_before"],
                "files_written": int(e["detail"].get("files_written") or 0),
                "files_deleted": int(e["detail"].get("files_deleted") or 0), "why": "cut short at the stop"}
    eol = state.get("end_of_life") or state.get("first_failure")
    if eol:
        rows = {int(r["cycle"]): r for r in cstore.drive(drive_id).read_cycles()}
        row = rows.get(int(eol["cycle"]))
        if row and row.get("free_before"):
            return {"cycle": int(row["cycle"]), "workload": row["workload"],
                    "free_before": int(float(row["free_before"])),
                    "files_written": int(float(row.get("write_files") or 0)),
                    "files_deleted": int(float(row.get("delete_files") or 0)), "why": "failed cycle"}
    return None


def verify_leftovers(campaign: Campaign, platform: Platform, vol: Volume, drive_id: str,
                     intake: dict[str, Any], info: dict[str, Any], *, max_files: int = 300,
                     max_bytes: int = 512 * MiB) -> dict[str, Any]:
    """Re-read the files of a past cycle that are still on the drive (data retention)."""
    workload = campaign.workloads[info["workload"]]
    cluster = int((intake.get("volume") or {}).get("cluster") or 32768)
    budget = fill_budget(int(info["free_before"]), campaign.cycle.fill_fraction, campaign.cycle.reserve_bytes)
    plan = plan_cycle(seed=campaign.seed, drive_id=drive_id, cycle=int(info["cycle"]), workload=workload,
                      budget_bytes=budget, cluster_size=cluster, chunk_size=campaign.cycle.chunk_size)
    cycle_dir = os.path.join(vol.root, PAYLOAD_DIR, plan.dirname)
    written = plan.files[int(info.get("files_deleted") or 0): int(info.get("files_written") or 0)]
    counts = {"checked": 0, "ok": 0, "corrupt": 0, "missing": 0, "unreadable": 0, "bytes": 0, "bits_wrong": 0}
    for spec in written:
        if counts["checked"] >= max_files or counts["bytes"] >= max_bytes:
            break
        path = os.path.join(cycle_dir, *spec.relpath.split("/"))
        try:
            check = verify_file(path, spec, cycle=plan.cycle, chunk_size=plan.chunk_size,
                                read_chunks=lambda p, n: platform.read_chunks(p, n, uncached=True))
        except OSError:
            counts["unreadable"] += 1
            counts["checked"] += 1
            continue
        counts["checked"] += 1
        counts["bytes"] += check.bytes_read
        counts["bits_wrong"] += check.bits_diff
        key = {FileStatus.OK: "ok", FileStatus.CORRUPT: "corrupt", FileStatus.MISSING: "missing"}[check.status]
        counts[key] += 1
    return {**counts, "cycle": plan.cycle, "workload": plan.workload, "why": info.get("why"),
            "planned_files": len(plan.files), "files_on_drive_expected": len(written)}


def post_check(campaign: Campaign, cstore: CampaignStore, platform: Platform, drive_id: str, *,
               kind: str = "posttest", progress=lambda text: None) -> dict[str, Any]:
    """``kind`` is "posttest" (on its test port) or "recheck" (on another port)."""
    intake = _intake(cstore, drive_id)
    ds = cstore.drive(drive_id)
    state = ds.load_state() or {}
    lock = _with_lock(cstore, drive_id)
    try:
        result: dict[str, Any] = {"time": iso(), "drive": drive_id, "kind": kind}
        vol, how = _locate(platform, drive_id, intake)
        result["found_by"] = how
        result["mount"] = vol.display
        loc = location_of(platform, vol)
        result["location"] = loc
        same = bool(loc.get("hub_key")) and (loc.get("hub_key"), loc.get("port_chain")) == \
            (intake.get("hub_key"), intake.get("port_chain"))
        result["on_test_port"] = same if loc.get("hub_key") else None
        if kind == "recheck" and same:
            raise ActionError(f"{drive_id} is still on its test port: move it to another port first")
        try:
            result["read_only"] = platform.is_read_only(vol.root)
        except OSError as exc:
            result["read_only"] = None
            result["read_only_error"] = str(exc)[:200]
        result["capacity"] = {"now": vol.total, "intake": intake.get("usable_bytes"), "fs": vol.fs}
        progress("identity, sentinel and file-system records")
        sres = SentinelChecker(platform, intake, campaign.sentinel).check(vol)
        result["sentinel"] = {"ok": sres.ok, "failed": sres.failed, "code": sres.code,
                              "detail": sres.detail[:400],
                              "components": {k: {kk: vv for kk, vv in v.items() if kk != "detail"}
                                             for k, v in sres.components.items()}}
        info = _leftover_cycle(cstore, drive_id, state)
        if info and vol.fs:
            progress(f"re-reading the files left from cycle {info['cycle']}")
            try:
                result["retention"] = verify_leftovers(campaign, platform, vol, drive_id, intake, info)
            except (OSError, KeyError, ValueError) as exc:
                result["retention"] = {"error": str(exc)[:200]}
        progress("fresh 64 MiB check")
        if result.get("read_only"):
            result["check"] = {"outcome": "read_only"}
        else:
            result["check"] = _summary(fresh_check(campaign, platform, vol, drive_id))
        result["passed"] = result["check"].get("outcome") == "pass" and sres.ok
    finally:
        lock.release()
    ds.event(kind, state.get("cycles_done", 0),
             **{k: v for k, v in result.items() if k not in ("time", "drive", "kind")})
    _record(cstore, kind, drive_id, result)
    cstore.operator(kind, drive=drive_id, passed=result["passed"])
    return result


def port_check(campaign: Campaign, cstore: CampaignStore, platform: Platform, spare_id: str,
               progress=lambda text: None) -> dict[str, Any]:
    """Run a fresh check with a spare drive and record it for the test port it sits in."""
    intake = _intake(cstore, spare_id)
    lock = _with_lock(cstore, spare_id)
    try:
        vol, _how = _locate(platform, spare_id, intake)
        loc = location_of(platform, vol)
        slot_drive = None
        for unit in campaign.test_drives():
            other = cstore.drive(unit).load_intake() or {}
            if loc.get("hub_key") and (other.get("hub_key"), other.get("port_chain")) == \
                    (loc.get("hub_key"), loc.get("port_chain")):
                slot_drive = unit
                break
        progress("fresh 64 MiB check with the spare")
        summary = _summary(fresh_check(campaign, platform, vol, spare_id))
    finally:
        lock.release()
    slot = campaign.slot_of(slot_drive) if slot_drive else None
    label = slot.label if slot else f"unmatched:{loc.get('hub_key', '?')}/{loc.get('port_chain', '?')}"
    result = {"time": iso(), "spare": spare_id, "slot": label, "slot_drive": slot_drive, "location": loc,
              "passed": summary.get("outcome") == "pass", "check": summary}
    _record(cstore, "portcheck", label, result)
    if slot_drive:
        cstore.drive(slot_drive).event("portcheck", 0, spare=spare_id, slot=label, passed=result["passed"],
                                       check=summary)
    cstore.operator("portcheck", spare=spare_id, slot=label, passed=result["passed"])
    return result


def retire(cstore: CampaignStore, drive_id: str, classification: str, reason: str = "") -> dict[str, Any]:
    """Final word on a drive that stopped: "failed" (end of life confirmed), or "port_fault"
    (the spare failed on the same port: F5, not counted against the drive)."""
    ds = cstore.drive(drive_id)
    raw = ds.load_state()
    if not raw:
        raise ActionError(f"{drive_id} has no state")
    state = DriveState.from_json(raw)
    if classification == "failed":
        state.status = Status.FAILED.value
        if state.end_of_life is None and state.first_failure is not None:
            state.end_of_life = dict(state.first_failure)
        if state.end_of_life is not None:
            state.end_of_life["confirmed"] = True
            state.end_of_life["retired_at"] = iso()
        ds.event("retired", state.cycles_done, (state.end_of_life or {}).get("code", ""),
                 classification="failed", reason=reason)
    elif classification == "recovered":
        # Passed the re-check on another port while the spare passed on its port: the
        # first failure stands (T), but the drive is not dead (end of life censored).
        ds.event("retired", state.cycles_done, (state.first_failure or {}).get("code", ""),
                 classification="recovered", reason=reason,
                 text="works again after the test: first failure kept, end of life censored")
        state.status = Status.CENSORED.value
        state.censored_at = iso()
        if state.end_of_life is not None:
            state.end_of_life = {**state.end_of_life, "confirmed": False, "revoked": "re-check passed"}
    elif classification == "port_fault":
        failed_at = (state.end_of_life or state.first_failure or {}).get("cycle", state.cycles_done)
        ds.event("port_fault", failed_at, "F5", reason=reason,
                 text="the spare failed on the same port: the failure is the port's, not the drive's")
        state.status = Status.CENSORED.value
        state.censored_at = iso()
        state.end_of_life = None
        if state.first_failure:
            state.first_failure["attributed"] = "port (F5)"
    else:
        raise ActionError(f"unknown classification {classification!r}")
    ds.save_state(state.to_json())
    cstore.operator("retire", drive=drive_id, classification=classification, reason=reason)
    return {"drive": drive_id, "status": state.status, "classification": classification}


def unsupported_message(exc: Exception) -> str:
    return str(exc) if isinstance(exc, (Unsupported, ActionError)) else f"{type(exc).__name__}: {exc}"
