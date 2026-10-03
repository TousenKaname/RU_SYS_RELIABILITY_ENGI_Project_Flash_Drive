"""Excel workbook of a campaign, organised by brand, capacity and workload.

Sheets: ``Drives`` (inventory, intake screening, current status), ``Cycles``
(every cycle), ``Events``, ``LifeTable`` (one row per drive), and ``ByGroup``
and ``ByWorkload`` summaries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from flashrel.analysis.lifedata import cycles_frame, events_frame, life_table
from flashrel.config import Campaign
from flashrel.recorder import DriveLog


def drives_frame(campaign: Campaign) -> pd.DataFrame:
    rows = []
    for drive_id, a in campaign.assignments.items():
        unit = campaign.inventory[drive_id]
        log = DriveLog(campaign.run_dir, drive_id)
        intake = json.loads(log.intake_path.read_text()) if log.intake_path.exists() else {}
        screen = intake.get("capacity_test", {})
        rows.append({
            "drive_id": drive_id, "group": unit.group.code, "brand": unit.brand,
            "model": unit.group.model, "capacity_gb": unit.capacity_gb,
            "housing": unit.group.housing, "host": a.host, "port": a.port,
            "usable_bytes": intake.get("total_bytes"), "filesystem": intake.get("filesystem"),
            "intake_write_mbps": screen.get("write_mbps"),
            "intake_read_mbps": screen.get("verify_mbps"),
            "intake_verdict": intake.get("verdict"),
            "status": log.load_state().status.value,
        })
    return pd.DataFrame(rows)


def write_workbook(campaign: Campaign, path: Path) -> Path:
    cycles = cycles_frame(campaign)
    life = life_table(campaign)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        drives_frame(campaign).to_excel(xl, sheet_name="Drives", index=False)
        cycles.to_excel(xl, sheet_name="Cycles", index=False)
        events_frame(campaign).to_excel(xl, sheet_name="Events", index=False)
        life.to_excel(xl, sheet_name="LifeTable", index=False)
        if not cycles.empty:
            ok = cycles[cycles["outcome"] == "pass"]
            (cycles.groupby(["group", "brand", "capacity_gb"])
                   .agg(drives=("drive_id", "nunique"), cycles=("cycle", "size"),
                        failed_cycles=("outcome", lambda s: int((s != "pass").sum())),
                        tb_written=("write_bytes", lambda s: s.sum() / 1e12))
                   .reset_index().to_excel(xl, sheet_name="ByGroup", index=False))
            (ok.groupby(["group", "workload"])
               .agg(cycles=("cycle", "size"), write_mbps_median=("write_mbps", "median"),
                    read_mbps_median=("verify_mbps", "median"),
                    hours_per_cycle=("duration_s", lambda s: s.median() / 3600))
               .reset_index().to_excel(xl, sheet_name="ByWorkload", index=False))
    return path
