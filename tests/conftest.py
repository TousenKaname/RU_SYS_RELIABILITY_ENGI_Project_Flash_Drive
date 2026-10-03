"""Shared fixtures: a small campaign and a temporary folder that stands in for a drive."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from flashrel.config import Campaign, load_campaign

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def campaign(tmp_path: Path) -> Campaign:
    """A two-drive campaign whose logs go to a temporary folder."""
    (tmp_path / "inventory.yaml").write_text(textwrap.dedent("""
        groups:
          T8: {brand: TestCo, model: T, capacity_gb: 8, interface: USB 2.0, housing: plastic,
               listed_write: "10", unit_price_usd: 5}
        units:
          T8: [T8-01, T8-02]
    """), encoding="utf-8")
    (tmp_path / "campaign.yaml").write_text(textwrap.dedent("""
        campaign: test
        seed: 7
        data_dir: data
        cycle: {fill_fraction: 0.9, reserve: 1MiB, chunk_size: 64KiB, retries: 1,
                retry_backoff_s: 0}
        workloads:
          small: {min_size: 4KiB, max_size: 64KiB, files_per_dir: 8}
          large: {min_size: 256KiB, max_size: 1MiB, files_per_dir: 4}
        rotation: [small, large]
        failure: {baseline_cycles: 2, slow_fraction: 0.5, slow_window: 2,
                  intermittent_limit: 2, intermittent_window: 5, reconnect_timeout_s: 1}
        assignments:
          T8-01: {host: H, port: P1}
          T8-02: {host: H, port: P2}
    """), encoding="utf-8")
    return load_campaign(tmp_path / "campaign.yaml")


@pytest.fixture
def drive(tmp_path: Path) -> Path:
    """An empty folder used as the mount point of a simulated drive."""
    path = tmp_path / "drive"
    path.mkdir()
    return path
