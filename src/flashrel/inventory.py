"""Drive inventory: which drives were bought and how each unit is named.

Every physical drive carries a short unit ID (for example ``A8-03``) written on
its label. The ID prefix names the purchase lot ("group"); all units of a
group share brand, model and nominal capacity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml


class InventoryError(ValueError):
    """Raised when the inventory file is missing data or inconsistent."""


@dataclass(frozen=True)
class DriveGroup:
    """A purchase lot of identical drives."""

    code: str
    brand: str
    model: str
    capacity_gb: float
    interface: str
    housing: str
    listed_write: str
    unit_price_usd: float


@dataclass(frozen=True)
class DriveUnit:
    """One physical drive."""

    drive_id: str
    group: DriveGroup

    @property
    def brand(self) -> str:
        return self.group.brand

    @property
    def capacity_gb(self) -> float:
        return self.group.capacity_gb


@dataclass(frozen=True)
class Inventory:
    groups: Mapping[str, DriveGroup]
    units: Mapping[str, DriveUnit]

    def __getitem__(self, drive_id: str) -> DriveUnit:
        try:
            return self.units[drive_id]
        except KeyError:
            raise InventoryError(f"unknown drive id {drive_id!r}") from None

    def __contains__(self, drive_id: object) -> bool:
        return drive_id in self.units

    def by_group(self, code: str) -> list[DriveUnit]:
        return [u for u in self.units.values() if u.group.code == code]


_GROUP_FIELDS = ("brand", "model", "capacity_gb", "interface", "housing", "listed_write",
                 "unit_price_usd")


def load_inventory(path: str | Path) -> Inventory:
    """Load ``inventory.yaml`` (groups plus the unit IDs in each group)."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    groups: dict[str, DriveGroup] = {}
    for code, spec in (raw.get("groups") or {}).items():
        missing = [f for f in _GROUP_FIELDS if f not in spec]
        if missing:
            raise InventoryError(f"{path}: group {code!r} is missing {', '.join(missing)}")
        groups[code] = DriveGroup(
            code=code,
            brand=str(spec["brand"]),
            model=str(spec["model"]),
            capacity_gb=float(spec["capacity_gb"]),
            interface=str(spec["interface"]),
            housing=str(spec["housing"]),
            listed_write=str(spec["listed_write"]),
            unit_price_usd=float(spec["unit_price_usd"]),
        )
    units: dict[str, DriveUnit] = {}
    for code, ids in (raw.get("units") or {}).items():
        if code not in groups:
            raise InventoryError(f"{path}: units listed for undefined group {code!r}")
        for drive_id in ids:
            if drive_id in units:
                raise InventoryError(f"{path}: duplicate drive id {drive_id!r}")
            units[drive_id] = DriveUnit(drive_id=str(drive_id), group=groups[code])
    if not units:
        raise InventoryError(f"{path}: no drive units defined")
    return Inventory(groups=groups, units=units)
