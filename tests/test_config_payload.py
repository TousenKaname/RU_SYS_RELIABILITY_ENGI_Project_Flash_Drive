from __future__ import annotations

import collections
import os

import pytest

from flashrel.config import ConfigError, parse_campaign
from flashrel.payload import fill_budget, iter_expected, plan_cycle, sentinel_bytes
from flashrel.platform.base import Platform
from flashrel.verify import CorruptionKind, FileStatus, verify_file


def test_plan_is_balanced_and_complete(campaign):
    assert len(campaign.test_drives()) == 20
    per_hub = {h: collections.Counter(campaign.group_of(s.drive).id for s in campaign.slots if s.hub == h)
               for h in campaign.hubs}
    assert per_hub == {"A": {"A16": 5, "A8": 4, "S8": 1}, "B": {"A16": 5, "A8": 4, "S8": 1}}
    assert set(campaign.spares) == {"A8-09", "A8-15"}
    assert not set(campaign.spares) & set(campaign.test_drives())
    assert campaign.prior["S8-01"].usb_serial == "03025117013126112727"
    assert campaign.duration_h == 120
    # run 1's rotation, so both runs can be pooled
    assert [campaign.workload_for_cycle(k).name for k in (1, 2, 3, 4)] == ["large", "medium", "small", "large"]
    order = campaign.start_order()
    assert len(order) == 20 and campaign.slot_of(order[0]).hub != campaign.slot_of(order[1]).hub


def test_config_rejects_double_slot(campaign):
    data = {"campaign": {"name": "x"}, "workloads": {"large": {"min_size": "1MiB", "max_size": "2MiB", "files_per_dir": 8}},
            "cycle": {"rotation": ["large"]},
            "group": [{"id": "G", "brand": "b", "capacity_gb": 8, "units": ["G-1", "G-2"]}],
            "slot": [{"hub": "A", "port": 1, "drive": "G-1"}, {"hub": "A", "port": 1, "drive": "G-2"}]}
    with pytest.raises(ConfigError):
        parse_campaign(data)


def test_plan_fills_budget_in_clusters(campaign):
    w = campaign.workloads["small"]
    budget = fill_budget(8_000_000_000, 0.9, 64 * 2**20)
    plan = plan_cycle(seed=1, drive_id="A8-06", cycle=3, workload=w, budget_bytes=budget,
                      cluster_size=32768, chunk_size=2**20)
    allocated = sum(-(-f.size // 32768) * 32768 for f in plan.files)
    assert budget - 32768 < allocated <= budget
    again = plan_cycle(seed=1, drive_id="A8-06", cycle=3, workload=w, budget_bytes=budget,
                       cluster_size=32768, chunk_size=2**20)
    assert again == plan
    other = plan_cycle(seed=1, drive_id="A8-06", cycle=3, workload=w, budget_bytes=budget,
                       cluster_size=32768, chunk_size=2**20, key_space="check-x")
    assert other.files[0].key != plan.files[0].key


def _write(tmp_path, spec, cycle=1):
    path = tmp_path / "f.bin"
    Platform().write_file(str(path), iter_expected(spec, cycle, 2**20), sync=True)
    return str(path)


def test_verify_detects_flip_truncation_and_missing(tmp_path, campaign):
    w = campaign.workloads["medium"]
    plan = plan_cycle(seed=7, drive_id="X", cycle=1, workload=w, budget_bytes=40 * 2**20,
                      cluster_size=32768, chunk_size=2**20)
    spec = max(plan.files, key=lambda f: f.size)
    path = _write(tmp_path, spec)
    reader = lambda p, n: Platform().read_chunks(p, n, uncached=False)  # noqa: E731
    assert verify_file(path, spec, cycle=1, chunk_size=2**20, read_chunks=reader).status is FileStatus.OK
    with open(path, "r+b") as fh:
        fh.seek(spec.size // 2)
        b = fh.read(1)
        fh.seek(-1, os.SEEK_CUR)
        fh.write(bytes([b[0] ^ 0x01]))
    check = verify_file(path, spec, cycle=1, chunk_size=2**20, read_chunks=reader)
    assert check.status is FileStatus.CORRUPT and check.bits_diff == 1
    assert check.diffs[0].kind is CorruptionKind.BIT_ERRORS
    with open(path, "r+b") as fh:
        fh.truncate(spec.size - 1000)
    check = verify_file(path, spec, cycle=1, chunk_size=2**20, read_chunks=reader)
    assert any(d.kind is CorruptionKind.TRUNCATED for d in check.diffs)
    os.remove(path)
    assert verify_file(path, spec, cycle=1, chunk_size=2**20, read_chunks=reader).status is FileStatus.MISSING


def test_stale_chunk_is_recognised(tmp_path, campaign):
    w = campaign.workloads["large"]
    old = plan_cycle(seed=7, drive_id="X", cycle=1, workload=w, budget_bytes=3 * 2**30,
                     cluster_size=32768, chunk_size=2**20)
    spec = old.files[0]
    new_spec = type(spec)(spec.index, spec.relpath, spec.size, spec.key)
    path = _write(tmp_path, spec, cycle=1)          # data of cycle 1 ...
    reader = lambda p, n: Platform().read_chunks(p, n, uncached=False)  # noqa: E731
    check = verify_file(path, new_spec, cycle=4, chunk_size=2**20, read_chunks=reader)  # ... read in cycle 4
    assert check.status is FileStatus.CORRUPT
    assert check.diffs[0].kind is CorruptionKind.STALE


def test_sentinel_content_is_reproducible():
    a = sentinel_bytes("A8-06", "abc", 1 << 20)
    assert a == sentinel_bytes("A8-06", "abc", 1 << 20) and len(a) == 1 << 20
    assert a != sentinel_bytes("A8-06", "abd", 1 << 20)


def test_a_design_frozen_by_an_earlier_version_still_loads(campaign):
    """2.0.0 froze temperature.serial_port, removed in 2.1.0; 2.1.0 added hub_window_s."""
    from flashrel.config import Campaign

    frozen = campaign.to_json()
    frozen["temperature"]["serial_port"] = ""
    del frozen["failure"]["hub_window_s"]
    again = Campaign.from_json(frozen)
    assert again.failure.hub_window_s == 30.0 and again.temperature.acpi == campaign.temperature.acpi
