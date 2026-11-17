"""Life table, Excel workbook and ZIP archive of a campaign.

The ZIP holds the whole data folder (every log as written by the rig). The workbook is a
convenience view: one sheet per record type, sorted by group, capacity and workload, as
the instructor asked ("put the data into an Excel file by type, by capacity, by size").
"""

from __future__ import annotations

import csv
import json
import os
import zipfile
from pathlib import Path
from typing import Any

from flashrel.config import Campaign
from flashrel.store import CampaignStore, read_csv, read_json, read_jsonl
from flashrel.timeutil import iso, utc_now
from flashrel.xlsx import write_xlsx

LIFE_FIELDS = (
    "drive", "group", "brand", "capacity_gb", "role", "hub", "port", "status", "cycles_done",
    "first_failure_cycle", "first_failure_code", "first_failure_hours", "first_failure_fdw",
    "first_failure_file_ops", "first_failure_event", "eol_cycle", "eol_code", "eol_confirmed",
    "eol_event", "censored", "censored_hours", "recoveries", "transients", "slow_workloads",
    "bytes_written", "bytes_read", "files_created", "files_deleted", "fdw", "usable_bytes",
    "prior_cycles", "prior_bytes_written", "usb_serial", "removal_policy", "hub_key", "port_chain",
)


def life_table(campaign: Campaign, cstore: CampaignStore) -> list[dict[str, Any]]:
    rows = []
    for unit in campaign.test_drives():
        ds = cstore.drive(unit)
        intake = ds.load_intake() or {}
        state = ds.load_state() or {}
        group = campaign.group_of(unit)
        slot = campaign.slot_of(unit)
        usable = int(intake.get("usable_bytes") or 0)
        cum = state.get("cum") or {}
        first = state.get("first_failure") or {}
        eol = state.get("end_of_life") or {}
        first_dose = first.get("dose") or {}
        censored = [e for e in ds.read_events() if e.get("type") == "censored"]
        prior = campaign.prior.get(unit)
        rows.append({
            "drive": unit, "group": group.id, "brand": group.brand, "capacity_gb": group.capacity_gb,
            "role": campaign.role(unit), "hub": slot.hub if slot else "", "port": slot.port if slot else "",
            "status": state.get("status", "not enrolled"), "cycles_done": state.get("cycles_done", 0),
            "first_failure_cycle": first.get("cycle"), "first_failure_code": first.get("code"),
            "first_failure_hours": first.get("hours_on_test"),
            "first_failure_fdw": round(first_dose.get("bytes_written", 0) / usable, 4) if first and usable else None,
            "first_failure_file_ops": (first_dose.get("files_created", 0) + first_dose.get("files_deleted", 0)) if first else None,
            "first_failure_event": 1 if first and not first.get("attributed") else 0,
            "eol_cycle": eol.get("cycle"), "eol_code": eol.get("code"), "eol_confirmed": eol.get("confirmed"),
            "eol_event": 1 if eol else 0, "censored": 1 if state.get("status") == "censored" else 0,
            "censored_hours": censored[-1]["detail"].get("hours_on_test") if censored else None,
            "recoveries": state.get("recoveries", 0), "transients": state.get("transients", 0),
            "slow_workloads": ";".join(state.get("slow_flagged") or []),
            "bytes_written": cum.get("bytes_written", 0), "bytes_read": cum.get("bytes_read", 0),
            "files_created": cum.get("files_created", 0), "files_deleted": cum.get("files_deleted", 0),
            "fdw": round(cum.get("bytes_written", 0) / usable, 4) if usable else None, "usable_bytes": usable,
            "prior_cycles": prior.cycles if prior else 0, "prior_bytes_written": prior.bytes_written if prior else 0,
            "usb_serial": intake.get("usb_serial"), "removal_policy": intake.get("removal_policy"),
            "hub_key": intake.get("hub_key"), "port_chain": intake.get("port_chain"),
        })
    return rows


def write_summary(campaign: Campaign, cstore: CampaignStore) -> dict[str, str]:
    rows = life_table(campaign, cstore)
    life_csv = cstore.dir / "life_table.csv"
    with open(life_csv, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LIFE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in LIFE_FIELDS})
    cycles, events, sentinel = [], [], []
    cyc_header: list[str] = []
    for unit in campaign.test_drives() + list(campaign.spares):
        ds = cstore.drive(unit)
        for r in read_csv(ds.cycles_csv):
            cyc_header = cyc_header or list(r.keys())
            cycles.append(r)
        for e in read_jsonl(ds.events_jsonl):
            events.append([e.get("time"), e.get("drive"), e.get("cycle"), e.get("type"), e.get("code"),
                           json.dumps(e.get("detail"), sort_keys=True)[:30000]])
        for s in read_csv(ds.sentinel_csv):
            if s.get("ok") in ("0", "False") or s.get("warnings") not in ("", "[]", None):
                sentinel.append(list(s.values()))
    order = {g: i for i, g in enumerate(campaign.groups)}
    workloads = {w: i for i, w in enumerate(campaign.cycle.rotation)}
    cycles.sort(key=lambda r: (order.get(r.get("group"), 9), r.get("drive"), int(r.get("cycle") or 0)))
    by_size = sorted(cycles, key=lambda r: (order.get(r.get("group"), 9), workloads.get(r.get("workload"), 9),
                                            r.get("drive"), int(r.get("cycle") or 0)))
    hub = read_csv(cstore.host_dir / "hub_load.csv")
    temp = read_csv(cstore.host_dir / "temperature.csv")
    xlsx = cstore.dir / f"{campaign.name}_workbook.xlsx"
    sheets = [
        ("Life table", list(LIFE_FIELDS), [[r.get(k) for k in LIFE_FIELDS] for r in rows]),
        ("Cycles by drive", cyc_header, [list(r.values()) for r in cycles]),
        ("Cycles by file size", cyc_header, [list(r.values()) for r in by_size]),
        ("Events", ["time", "drive", "cycle", "type", "code", "detail"], events),
        ("Sentinel findings", list(read_csv_header(cstore, campaign)), sentinel),
        ("Hub load", list(hub[0].keys()) if hub else ["time"], [list(r.values()) for r in hub]),
        ("Temperature", list(temp[0].keys()) if temp else ["time"], [list(r.values()) for r in temp]),
    ]
    write_xlsx(str(xlsx), sheets)
    return {"life_table": str(life_csv), "workbook": str(xlsx)}


def read_csv_header(cstore: CampaignStore, campaign: Campaign) -> list[str]:
    from flashrel.store import SENTINEL_FIELDS

    return list(SENTINEL_FIELDS)


def export_zip(campaign: Campaign, cstore: CampaignStore, exports_dir: str | Path) -> dict[str, Any]:
    summary = write_summary(campaign, cstore)
    exports_dir = Path(exports_dir)
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().astimezone().strftime("%Y%m%d-%H%M")
    target = exports_dir / f"{campaign.name}_{stamp}.zip"
    n_files = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(cstore.dir):
            for name in files:
                if name.endswith((".lock", ".tmp", ".pid")) or ".tmp" in name:
                    continue
                full = os.path.join(base, name)
                rel = os.path.relpath(full, cstore.dir.parent)
                try:
                    z.write(full, rel)
                    n_files += 1
                except OSError:
                    continue
    cstore.operator("export", zip=str(target), files=n_files)
    return {"zip": str(target), "files": n_files, "size": target.stat().st_size, "time": iso(), **summary}
