"""Helpers shared by several test modules."""

from __future__ import annotations

from flashrel.config import Campaign, WorkloadSpec
from flashrel.payload import plan_cycle
from flashrel.system.volumes import VolumeUsage
from flashrel.units import KiB, MiB


def capped_planner(campaign: Campaign, workload: WorkloadSpec, cycle: int = 1,
                   budget: int = 2 * MiB, drive_id: str = "T8-01"):
    """Planner with a fixed budget, so tests never fill the real disk."""
    def planner(volume: VolumeUsage):
        return plan_cycle(seed=campaign.seed, drive_id=drive_id, cycle=cycle, workload=workload,
                          budget_bytes=budget, cluster_size=4 * KiB,
                          chunk_size=campaign.cycle.chunk_size)
    return planner
