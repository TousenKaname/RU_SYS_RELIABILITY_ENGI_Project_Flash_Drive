"""Intake: find candidate drives, format them, write identity and sentinel, record a baseline.

Safety comes first, because formatting the wrong disk destroys data. A volume is
formatted only when every check passes: it sits on the USB bus, it is not on the
system disk, its size fits the drive's group (+-15 % of the nominal capacity), it is
not already enrolled as another drive, and the operator picked exactly this volume
(its USB serial number) for exactly this drive ID. A reused run-1 drive must also show
its run-1 serial number.
"""

from __future__ import annotations

import base64
import os
import secrets
from typing import Any

from flashrel import __version__
from flashrel.config import Campaign
from flashrel.failure import DriveState, Status
from flashrel.payload import identity_bytes, sentinel_bytes
from flashrel.platform import Platform, Unsupported, Volume
from flashrel.sentinel import (IDENTITY_FILE, SENTINEL_FILE, SentinelChecker, intake_baseline,
                               read_identity, sha256)
from flashrel.store import CampaignStore, atomic_write_json, read_json
from flashrel.timeutil import iso

SIZE_TOLERANCE = 0.15


class IntakeError(RuntimeError):
    pass


def enrolled_drives(campaign: Campaign, cstore: CampaignStore) -> dict[str, dict[str, Any]]:
    out = {}
    for unit in campaign.all_units():
        intake = read_json(cstore.dir / "drives" / unit / "intake.json")
        if intake:
            out[unit] = intake
    return out


def _inventory_by_serial(cstore: CampaignStore) -> dict[str, dict[str, Any]]:
    inv = read_json(cstore.host_dir / "inventory.json", {}) or {}
    return {d.get("serial", ""): d for d in inv.get("disks", []) if d.get("serial")}


def candidates(campaign: Campaign, cstore: CampaignStore, platform: Platform) -> list[dict[str, Any]]:
    """USB volumes that could be enrolled, with every fact the operator needs to decide."""
    enrolled = enrolled_drives(campaign, cstore)
    tokens = {i.get("token"): d for d, i in enrolled.items()}
    serials = {i.get("usb_serial"): d for d, i in enrolled.items()
               if i.get("usb_serial") and i.get("serial_unique", True)}
    inv = _inventory_by_serial(cstore)
    out = []
    for vol in platform.list_volumes():
        info = platform.storage_info(vol.root)
        if info is None or info.bus != "USB" or info.system_disk:
            continue
        ident = read_identity(vol) if vol.fs else None
        enrolled_as = ""
        if ident and ident.get("campaign") == campaign.name and ident.get("token") in tokens:
            enrolled_as = tokens[ident["token"]]
        elif info.serial and info.serial in serials and not vol.fs:
            enrolled_as = serials[info.serial]  # lost its file system: known by serial only
        disk = inv.get(info.serial, {})
        out.append({
            "root": vol.root, "letter": vol.letter, "guid": vol.guid, "label": vol.label,
            "fs": vol.fs, "total": vol.total, "free": vol.free, "cluster": vol.cluster,
            "serial": info.serial, "vendor": info.vendor, "product": info.product,
            "removable": info.removable_media, "enrolled_as": enrolled_as,
            "old_identity": (ident or {}).get("drive_id", "") if not enrolled_as else "",
            "hub_key": disk.get("hub_key", ""), "port_chain": disk.get("port_chain", ""),
            "removal_policy": disk.get("removal_policy"),
        })
    return out


def _find_volume(platform: Platform, guid: str, root: str) -> Volume | None:
    for vol in platform.list_volumes():
        if (guid and vol.guid == guid) or (root and vol.root == root):
            return vol
    return None


def preflight(campaign: Campaign, cstore: CampaignStore, platform: Platform, drive_id: str,
              vol: Volume, serial: str) -> tuple[list[str], list[str]]:
    """(blocking problems, warnings) for enrolling ``vol`` as ``drive_id``."""
    problems: list[str] = []
    warnings: list[str] = []
    if drive_id not in campaign.all_units():
        problems.append(f"{drive_id} is not a unit of campaign {campaign.name}")
        return problems, warnings
    info = platform.storage_info(vol.root)
    if info is None:
        problems.append("cannot query the storage device (bus type, serial number)")
        return problems, warnings
    if info.bus != "USB":
        problems.append(f"not a USB device (bus {info.bus})")
    if info.system_disk:
        problems.append("this volume is on the system disk")
    if serial and info.serial != serial:
        problems.append("the selected volume changed (serial number differs); pick it again")
    nominal = campaign.nominal_bytes(drive_id)
    size = vol.total or 0
    if size and not (1 - SIZE_TOLERANCE) * nominal <= size <= (1 + SIZE_TOLERANCE) * nominal:
        problems.append(f"size {size / 1e9:.2f} GB does not fit a {nominal / 1e9:.3g} GB drive "
                        f"({campaign.group_of(drive_id).id}): is this the stick labelled {drive_id}?")
    enrolled = enrolled_drives(campaign, cstore)
    ident = read_identity(vol) if vol.fs else None
    for other, intake in enrolled.items():
        if other == drive_id:
            continue
        if ident and ident.get("campaign") == campaign.name and ident.get("token") == intake.get("token"):
            problems.append(f"this stick is already enrolled as {other}")
        elif info.serial and intake.get("usb_serial") == info.serial:
            warnings.append(f"USB serial {info.serial} was also reported by {other}: serial numbers "
                            "of this lot are not unique, so drives are told apart by identity file "
                            "and port only")
    prior = campaign.prior.get(drive_id)
    if prior and prior.usb_serial and info.serial and prior.usb_serial != info.serial:
        problems.append(f"{drive_id} is a run-1 drive with USB serial {prior.usb_serial}, but this "
                        f"stick reports {info.serial}: wrong stick?")
    if drive_id in enrolled:
        warnings.append(f"{drive_id} is already enrolled; enrolling again replaces its baseline")
    if not platform.supports_format or not platform.is_admin():
        if vol.fs != "exFAT" or (vol.cluster and vol.cluster != 32768):
            problems.append("no administrator rights to format: format it by hand first "
                            "(exFAT, allocation unit 32 KB, quick format), then retry")
    return problems, warnings


def enroll(campaign: Campaign, cstore: CampaignStore, platform: Platform, drive_id: str, *,
           guid: str = "", root: str = "", serial: str = "", progress=lambda text: None) -> dict[str, Any]:
    vol = _find_volume(platform, guid, root)
    if vol is None:
        raise IntakeError("the selected volume is no longer present")
    problems, warnings = preflight(campaign, cstore, platform, drive_id, vol, serial)
    if problems:
        raise IntakeError("; ".join(problems))
    info = platform.storage_info(vol.root)
    assert info is not None
    fmt: dict[str, Any] = {"method": "none"}
    if platform.supports_format and platform.is_admin():
        progress("formatting (exFAT, 32 KiB clusters, quick)")
        try:
            fmt = {"method": "format-volume", **platform.format_volume(vol, drive_id)}
        except (Unsupported, OSError, RuntimeError) as exc:
            raise IntakeError(f"format failed: {exc}") from None
        vol = _wait_volume(platform, vol, info.serial)
    else:
        leftovers = [n for n in os.listdir(vol.root) if n.lower() not in ("system volume information",)]
        if leftovers:
            raise IntakeError(f"the volume is not empty ({', '.join(leftovers[:5])}); format it by hand")
        fmt = {"method": "manual (no administrator rights)"}
    if vol.fs != "exFAT":
        warnings.append(f"file system is {vol.fs!r}, not exFAT")
    group = campaign.group_of(drive_id)
    token = secrets.token_hex(8)
    identity = {"drive_id": drive_id, "campaign": campaign.name, "group": group.id,
                "token": token, "enrolled_at": iso(), "software": f"flashrel {__version__}",
                "nominal_gb": group.capacity_gb}
    ident_bytes = identity_bytes(identity)
    sentinel_size = campaign.sentinel.file_size
    sent_bytes = sentinel_bytes(drive_id, token, sentinel_size)
    progress("writing identity and sentinel files")
    platform.write_file(os.path.join(vol.root, IDENTITY_FILE), [ident_bytes], sync=True)
    platform.write_file(os.path.join(vol.root, SENTINEL_FILE),
                        [sent_bytes[i:i + 1024 * 1024] for i in range(0, len(sent_bytes), 1024 * 1024)],
                        sync=True)
    vol = _find_volume(platform, vol.guid, vol.root) or vol
    slot = campaign.slot_of(drive_id)
    prior = campaign.prior.get(drive_id)
    record: dict[str, Any] = {
        "drive_id": drive_id, "campaign": campaign.name, "group": group.id, "brand": group.brand,
        "model": group.model, "capacity_gb": group.capacity_gb, "role": campaign.role(drive_id),
        "hub": slot.hub if slot else "", "port": slot.port if slot else 0,
        "enrolled_at": iso(), "software": __version__, "token": token,
        "identity_b64": base64.b64encode(ident_bytes).decode("ascii"),
        "identity_sha256": sha256(ident_bytes), "sentinel_size": sentinel_size,
        "sentinel_sha256": sha256(sent_bytes), "volume": vol.to_json(), "usable_bytes": vol.total,
        "usb_serial": info.serial, "serial_unique": True, "vendor": info.vendor,
        "product": info.product, "revision": info.revision, "bus": info.bus,
        "disk_number": info.disk_number, "format": fmt,
        "prior": prior.__dict__ if prior else None, "warnings": warnings,
    }
    progress("reading the files back around the cache")
    checker = SentinelChecker(platform, {**record, "exfat": {}}, campaign.sentinel)
    first = checker.check(vol)
    if not first.ok:
        raise IntakeError(f"the files just written do not read back correctly: {first.detail}")
    progress("recording the exFAT baseline")
    record["exfat"] = intake_baseline(platform, vol) if campaign.sentinel.raw_checks else {"ok": False,
                                                                                           "error": "disabled"}
    if not record["exfat"].get("ok"):
        warnings.append(f"raw file-system checks off for this drive: {record['exfat'].get('error')}")
    progress("reading USB location and removal policy")
    record.update(_device_facts(platform, platform.storage_info(vol.root) or info, warnings))
    _hub_consistency(campaign, cstore, record, warnings)
    _mark_duplicate_serials(campaign, cstore, record)
    ds = cstore.drive(drive_id)
    ds.save_intake(record)
    state = DriveState(drive_id, status=Status.ENROLLED.value)
    ds.save_state(state.to_json())
    ds.event("enrolled", 0, role=record["role"], hub=record["hub"], port=record["port"],
             usb_serial=info.serial, usable_bytes=vol.total, removal_policy=record.get("removal_policy"),
             hub_key=record.get("hub_key"), raw_checks=record["exfat"].get("ok"), warnings=warnings)
    cstore.operator("enroll", drive=drive_id, serial=info.serial, letter=vol.letter, warnings=warnings)
    return record


def _wait_volume(platform: Platform, before: Volume, serial: str, timeout: float = 60.0) -> Volume:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for vol in platform.list_volumes():
            if vol.guid == before.guid or (vol.letter and vol.letter == before.letter):
                if vol.fs:
                    return vol
        time.sleep(1.0)
    raise IntakeError("the volume did not come back after the format")


def match_disk(disks: list[dict[str, Any]], disk_number: int | None, serial: str) -> dict[str, Any] | None:
    """The inventory entry of a stick: by disk number (unique at this moment), the serial
    only as a cross-check; by serial alone only when exactly one disk carries it."""
    if disk_number is not None:
        disk = next((d for d in disks if d.get("number") == disk_number), None)
        if disk is not None and (not serial or not disk.get("serial") or disk.get("serial") == serial):
            return disk
        return None
    same = [d for d in disks if serial and d.get("serial") == serial]
    return same[0] if len(same) == 1 else None


def _device_facts(platform: Platform, info, warnings: list[str]) -> dict[str, Any]:
    if not platform.supports_inventory:
        return {}
    try:
        disks = platform.inventory()
    except Exception as exc:  # PowerShell missing or blocked: the rig still works
        warnings.append(f"USB location unavailable: {exc}")
        return {}
    disk = match_disk(disks, info.disk_number, info.serial)
    if disk is None:
        warnings.append("this stick could not be matched in the USB inventory (location unknown; "
                        "the recovery protocol will not restart it)")
        return {}
    policy = disk.get("removal_policy")
    if policy not in (None, 3):
        warnings.append(f"Windows removal policy is {policy}, not 3 (quick removal): this port "
                        "is treated differently (see run 1, port W3); move the hub to another port")
    return {k: disk.get(k) for k in ("usb_instance", "disk_instance", "hub_instance", "hub_name",
                                     "location_info", "location_paths", "hub_key", "port_chain",
                                     "removal_policy", "vid_pid", "model")}


def _hub_consistency(campaign: Campaign, cstore: CampaignStore, record: dict[str, Any],
                     warnings: list[str]) -> None:
    """Drives planned for one hub must share one hub key; the two hubs must differ."""
    key = record.get("hub_key")
    if not key or not record.get("hub"):
        return
    for other, intake in enrolled_drives(campaign, cstore).items():
        if other == record["drive_id"] or not intake.get("hub_key") or not intake.get("hub"):
            continue
        same_hub = intake["hub"] == record["hub"]
        same_key = intake["hub_key"] == key
        if same_hub and not same_key:
            warnings.append(f"{other} (also hub {record['hub']}) sits behind another PC port "
                            f"({intake['hub_key']}): is this drive in the right hub?")
            return
        if not same_hub and same_key:
            warnings.append(f"{other} (hub {intake['hub']}) sits behind the same PC port: are both "
                            "hubs plugged into the same port or daisy-chained?")
            return


def _mark_duplicate_serials(campaign: Campaign, cstore: CampaignStore, record: dict[str, Any]) -> None:
    serial = record.get("usb_serial")
    if not serial:
        record["serial_unique"] = False
        return
    for other, intake in enrolled_drives(campaign, cstore).items():
        if other != record["drive_id"] and intake.get("usb_serial") == serial:
            record["serial_unique"] = False
            intake["serial_unique"] = False
            atomic_write_json(cstore.dir / "drives" / other / "intake.json", intake)
