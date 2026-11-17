"""Campaign definition: TOML file -> validated, frozen dataclasses.

The operator never edits the configuration during a test. When the test starts, the
controller writes the parsed campaign to ``campaign.json`` in the data folder, and the
drive workers read that frozen copy, so every drive runs the same, recorded design.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flashrel.units import KiB, MiB, parse_size


class ConfigError(ValueError):
    """The campaign file is inconsistent."""


@dataclass(frozen=True)
class Workload:
    name: str
    min_size: int
    max_size: int
    files_per_dir: int


@dataclass(frozen=True)
class CycleSpec:
    fill_fraction: float = 0.90
    reserve_bytes: int = 64 * MiB
    chunk_size: int = 1 * MiB
    fsync: str = "per_file"
    readback: str = "uncached"
    retries: int = 2
    retry_backoff_s: float = 5.0
    rotation: tuple[str, ...] = ("large", "medium", "small")


@dataclass(frozen=True)
class SentinelSpec:
    interval_s: float = 600.0
    file_size: int = 1 * MiB
    raw_checks: bool = True
    confirm_reads: int = 3
    confirm_gap_s: float = 5.0


@dataclass(frozen=True)
class FailurePolicy:
    baseline_cycles: int = 2
    slow_fraction: float = 0.5
    slow_window: int = 3
    reconnect_timeout_s: float = 120.0
    hang_timeout_s: float = 600.0
    intermittent_limit: int = 3
    intermittent_window: int = 10
    device_reset: bool = True
    check_bytes: int = 64 * MiB
    hub_event_drives: int = 3


@dataclass(frozen=True)
class TemperatureSpec:
    manual: bool = True
    acpi: bool = True
    serial_port: str = ""


@dataclass(frozen=True)
class Group:
    id: str
    brand: str
    model: str
    capacity_gb: float
    interface: str
    lot: str
    units: tuple[str, ...]


@dataclass(frozen=True)
class Prior:
    cycles: int = 0
    bytes_written: int = 0
    usb_serial: str = ""
    note: str = ""


@dataclass(frozen=True)
class Slot:
    hub: str
    port: int
    drive: str

    @property
    def label(self) -> str:
        return f"{self.hub}{self.port:02d}"


@dataclass(frozen=True)
class Campaign:
    name: str
    title: str
    seed: int
    duration_h: float
    stagger_s: float
    cycle: CycleSpec
    workloads: dict[str, Workload]
    sentinel: SentinelSpec
    failure: FailurePolicy
    temperature: TemperatureSpec
    hubs: tuple[str, ...]
    ports_per_hub: int
    hub_model: str
    groups: dict[str, Group]
    prior: dict[str, Prior]
    slots: tuple[Slot, ...]
    spares: tuple[str, ...]
    sim: dict[str, Any] = field(default_factory=dict)  # non-empty only in simulation mode

    # -- lookups ----------------------------------------------------------------------------
    def workload_for_cycle(self, cycle: int) -> Workload:
        if cycle < 1:
            raise ValueError("cycles are numbered from 1")
        return self.workloads[self.cycle.rotation[(cycle - 1) % len(self.cycle.rotation)]]

    def group_of(self, drive_id: str) -> Group:
        for group in self.groups.values():
            if drive_id in group.units:
                return group
        raise KeyError(f"{drive_id} is not a unit of this campaign")

    def slot_of(self, drive_id: str) -> Slot | None:
        return next((s for s in self.slots if s.drive == drive_id), None)

    def test_drives(self) -> list[str]:
        """Drives under test in plan order (hub A port 1 ... hub B port 10)."""
        order = {h: i for i, h in enumerate(self.hubs)}
        return [s.drive for s in sorted(self.slots, key=lambda s: (order[s.hub], s.port))]

    def all_units(self) -> list[str]:
        return [u for g in self.groups.values() for u in g.units]

    def nominal_bytes(self, drive_id: str) -> int:
        return int(self.group_of(drive_id).capacity_gb * 1000**3)

    def role(self, drive_id: str) -> str:
        if drive_id in self.spares:
            return "spare"
        return "test" if self.slot_of(drive_id) else "unused"

    def start_order(self) -> list[str]:
        """Workers start alternating between hubs (A1, B1, A2, B2, ...)."""
        by_hub = {h: [s for s in sorted(self.slots, key=lambda s: s.port) if s.hub == h]
                  for h in self.hubs}
        order: list[str] = []
        for i in range(self.ports_per_hub):
            for hub in self.hubs:
                if i < len(by_hub[hub]):
                    order.append(by_hub[hub][i].drive)
        return order

    # -- serialization ----------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Campaign:
        return cls(
            name=d["name"], title=d["title"], seed=int(d["seed"]),
            duration_h=float(d["duration_h"]), stagger_s=float(d["stagger_s"]),
            cycle=CycleSpec(**{**d["cycle"], "rotation": tuple(d["cycle"]["rotation"])}),
            workloads={k: Workload(**v) for k, v in d["workloads"].items()},
            sentinel=SentinelSpec(**d["sentinel"]), failure=FailurePolicy(**d["failure"]),
            temperature=TemperatureSpec(**d["temperature"]), hubs=tuple(d["hubs"]),
            ports_per_hub=int(d["ports_per_hub"]), hub_model=d.get("hub_model", ""),
            groups={k: Group(**{**v, "units": tuple(v["units"])}) for k, v in d["groups"].items()},
            prior={k: Prior(**v) for k, v in d.get("prior", {}).items()},
            slots=tuple(Slot(**s) for s in d["slots"]), spares=tuple(d.get("spares", ())),
            sim=dict(d.get("sim") or {}),
        )


# -- loading ----------------------------------------------------------------------------------
def _get(table: dict[str, Any], key: str, default: Any = None, *, required: bool = False) -> Any:
    if key in table:
        return table[key]
    if required:
        raise ConfigError(f"missing key {key!r}")
    return default


def parse_campaign(data: dict[str, Any]) -> Campaign:
    c = _get(data, "campaign", {}, required=True)
    cy = data.get("cycle", {})
    rotation = tuple(cy.get("rotation", ("large", "medium", "small")))
    cycle = CycleSpec(
        fill_fraction=float(cy.get("fill_fraction", 0.9)),
        reserve_bytes=parse_size(cy.get("reserve", "64MiB")),
        chunk_size=parse_size(cy.get("chunk_size", "1MiB")),
        fsync=str(cy.get("fsync", "per_file")),
        readback=str(cy.get("readback", "uncached")),
        retries=int(cy.get("retries", 2)),
        retry_backoff_s=float(cy.get("retry_backoff_s", 5)),
        rotation=rotation,
    )
    workloads = {
        name: Workload(name, parse_size(w["min_size"]), parse_size(w["max_size"]),
                       int(w["files_per_dir"]))
        for name, w in data.get("workloads", {}).items()
    }
    s = data.get("sentinel", {})
    sentinel = SentinelSpec(
        interval_s=float(s.get("interval_min", 10)) * 60.0,
        file_size=parse_size(s.get("file_size", "1MiB")),
        raw_checks=bool(s.get("raw_checks", True)),
        confirm_reads=int(s.get("confirm_reads", 3)),
        confirm_gap_s=float(s.get("confirm_gap_s", 5)),
    )
    f = data.get("failure", {})
    failure = FailurePolicy(
        baseline_cycles=int(f.get("baseline_cycles", 2)),
        slow_fraction=float(f.get("slow_fraction", 0.5)),
        slow_window=int(f.get("slow_window", 3)),
        reconnect_timeout_s=float(f.get("reconnect_timeout_s", 120)),
        hang_timeout_s=float(f.get("hang_timeout_s", 600)),
        intermittent_limit=int(f.get("intermittent_limit", 3)),
        intermittent_window=int(f.get("intermittent_window", 10)),
        device_reset=bool(f.get("device_reset", True)),
        check_bytes=parse_size(f.get("check_size", "64MiB")),
        hub_event_drives=int(f.get("hub_event_drives", 3)),
    )
    t = data.get("temperature", {})
    temperature = TemperatureSpec(manual=bool(t.get("manual", True)), acpi=bool(t.get("acpi", True)),
                                  serial_port=str(t.get("serial_port", "")))
    h = data.get("hubs", {})
    groups = {}
    for g in data.get("group", []):
        group = Group(id=str(g["id"]), brand=str(g["brand"]), model=str(g.get("model", "")),
                      capacity_gb=float(g["capacity_gb"]), interface=str(g.get("interface", "")),
                      lot=str(g.get("lot", "")), units=tuple(g["units"]))
        if group.id in groups:
            raise ConfigError(f"duplicate group {group.id}")
        groups[group.id] = group
    prior = {k: Prior(cycles=int(v.get("cycles", 0)), bytes_written=int(v.get("bytes_written", 0)),
                      usb_serial=str(v.get("usb_serial", "")), note=str(v.get("note", "")))
             for k, v in data.get("prior", {}).items()}
    slots = tuple(Slot(str(s["hub"]), int(s["port"]), str(s["drive"])) for s in data.get("slot", []))
    plan = data.get("plan", {})
    campaign = Campaign(
        name=str(_get(c, "name", required=True)), title=str(c.get("title", "")),
        seed=int(c.get("seed", 0)), duration_h=float(c.get("duration_h", 120)),
        stagger_s=float(c.get("stagger_s", 15)), cycle=cycle, workloads=workloads,
        sentinel=sentinel, failure=failure, temperature=temperature,
        hubs=tuple(h.get("names", ("A", "B"))), ports_per_hub=int(h.get("ports", 10)),
        hub_model=str(h.get("model", "")), groups=groups, prior=prior, slots=slots,
        spares=tuple(plan.get("spares", ())),
    )
    validate(campaign)
    return campaign


def validate(c: Campaign) -> None:
    if not c.name.replace("-", "").replace("_", "").isalnum():
        raise ConfigError(f"campaign name must be alphanumeric: {c.name!r}")
    if not 0.0 < c.cycle.fill_fraction < 1.0:
        raise ConfigError("fill_fraction must lie between 0 and 1")
    if c.cycle.chunk_size % 4096:
        raise ConfigError("chunk_size must be a multiple of 4 KiB (unbuffered reads)")
    for w in c.workloads.values():
        if not 0 < w.min_size <= w.max_size or w.files_per_dir < 1:
            raise ConfigError(f"workload {w.name}: bad sizes")
    for name in c.cycle.rotation:
        if name not in c.workloads:
            raise ConfigError(f"rotation names unknown workload {name!r}")
    units = c.all_units()
    if len(units) != len(set(units)):
        raise ConfigError("a unit ID appears twice")
    seen_slots: set[tuple[str, int]] = set()
    seen_drives: set[str] = set()
    for s in c.slots:
        if s.hub not in c.hubs:
            raise ConfigError(f"slot names unknown hub {s.hub!r}")
        if not 1 <= s.port <= c.ports_per_hub:
            raise ConfigError(f"slot {s.hub}{s.port}: port outside 1..{c.ports_per_hub}")
        if (s.hub, s.port) in seen_slots:
            raise ConfigError(f"slot {s.hub}{s.port} assigned twice")
        if s.drive not in units:
            raise ConfigError(f"slot {s.hub}{s.port}: unknown drive {s.drive}")
        if s.drive in seen_drives:
            raise ConfigError(f"drive {s.drive} has two slots")
        seen_slots.add((s.hub, s.port))
        seen_drives.add(s.drive)
    for spare in c.spares:
        if spare not in units:
            raise ConfigError(f"unknown spare {spare}")
        if spare in seen_drives:
            raise ConfigError(f"spare {spare} also has a test slot")
    for drive in c.prior:
        if drive not in units:
            raise ConfigError(f"prior record for unknown drive {drive}")


def load_campaign(path: str | Path) -> Campaign:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return parse_campaign(data)


def for_simulation(c: Campaign, *, duration_h: float = 0.5, speed: float = 1.0) -> Campaign:
    """A scaled-down copy of a campaign for the simulated rig (development and demos).

    Simulated drives hold tens of MiB, so the workloads shrink by about the same factor
    as the capacities (8 GB -> 48 MiB) and the timers by much more.
    """
    workloads = {
        "large": Workload("large", 4 * MiB, 12 * MiB, 8),
        "medium": Workload("medium", 128 * KiB, 2 * MiB, 64),
        "small": Workload("small", 4 * KiB, 48 * KiB, 128),
    }
    # Simulated sticks hold 6 MiB per nominal GB (8 GB -> 48 MiB, 16 GB -> 96 MiB).
    groups = {k: dataclasses.replace(g, capacity_gb=g.capacity_gb * 6 * MiB / 1e9)
              for k, g in c.groups.items()}
    return dataclasses.replace(
        c,
        name=c.name + "-sim",
        title=c.title + " (SIMULATION)",
        groups=groups,
        prior={k: dataclasses.replace(p, usb_serial="") for k, p in c.prior.items()},
        duration_h=duration_h,
        stagger_s=1.0,
        cycle=dataclasses.replace(c.cycle, reserve_bytes=1 * MiB, retry_backoff_s=0.2),
        workloads={k: workloads.get(k, v) for k, v in c.workloads.items()},
        sentinel=dataclasses.replace(c.sentinel, interval_s=20.0, file_size=256 * KiB,
                                     confirm_gap_s=0.5),
        failure=dataclasses.replace(c.failure, reconnect_timeout_s=8.0, hang_timeout_s=90.0,
                                    check_bytes=2 * MiB),
        sim={"speed": speed},
    )
