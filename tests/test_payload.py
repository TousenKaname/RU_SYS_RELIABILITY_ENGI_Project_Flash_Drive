from __future__ import annotations

import pytest

from flashrel.config import WorkloadSpec
from flashrel.payload import (
    HEADER,
    MAGIC,
    expected_chunk,
    fill_budget,
    iter_expected,
    plan_cycle,
    stable_key,
)
from flashrel.units import GiB, KiB, MiB, parse_size

SMALL = WorkloadSpec("small", min_size=4 * KiB, max_size=256 * KiB, files_per_dir=256)


def plan(**overrides):
    args = dict(seed=1, drive_id="A8-01", cycle=3, workload=SMALL, budget_bytes=64 * MiB,
                cluster_size=32 * KiB, chunk_size=1 * MiB)
    args.update(overrides)
    return plan_cycle(**args)


def test_parse_size_units():
    assert parse_size("4KiB") == 4096
    assert parse_size("1.5GiB") == int(1.5 * GiB)
    assert parse_size("2 MB") == 2_000_000
    assert parse_size(512) == 512
    with pytest.raises(ValueError):
        parse_size("12 parsecs")


def test_stable_key_is_deterministic_and_distinct():
    assert stable_key(1, "A8-01", 3) == stable_key(1, "A8-01", 3)
    assert stable_key(1, "A8-01", 3) != stable_key(1, "A8-01", 4)
    assert 0 <= stable_key("x") < 2**64


def test_plan_is_reproducible():
    assert plan() == plan()
    assert plan(cycle=4).files != plan().files


def test_plan_respects_size_bounds_and_cluster_budget():
    p = plan()
    cluster = 32 * KiB
    allocated = sum(-(-f.size // cluster) * cluster for f in p.files)
    assert allocated <= 64 * MiB
    assert allocated > 64 * MiB - 256 * KiB - cluster  # the last file soaks up the remainder
    assert all(SMALL.min_size <= f.size <= SMALL.max_size for f in p.files[:-1])
    assert len({f.key for f in p.files}) == len(p.files)


def test_plan_spreads_files_over_directories():
    p = plan()
    dirs = {f.relpath.split("/")[0] for f in p.files}
    assert len(dirs) == -(-len(p.files) // SMALL.files_per_dir)


def test_empty_plan_when_budget_too_small():
    assert plan(budget_bytes=1024).files == ()


def test_fill_budget():
    assert fill_budget(1000 * MiB, 0.9, 64 * MiB) == int(900 * MiB)
    assert fill_budget(100 * MiB, 0.9, 64 * MiB) == 36 * MiB
    assert fill_budget(10 * MiB, 0.9, 64 * MiB) == 0


def test_chunks_carry_header_and_cover_the_file():
    f = plan().files[0]
    chunk = expected_chunk(f, 3, 0, 1 * MiB)
    magic, version, key, cycle, index, chunk_index = HEADER.unpack_from(chunk)
    assert (magic, key, cycle, index, chunk_index) == (MAGIC, f.key, 3, f.index, 0)
    assert sum(len(c) for c in iter_expected(f, 3, 64 * KiB)) == f.size
    assert expected_chunk(f, 3, 0, 1 * MiB) == chunk
    with pytest.raises(IndexError):
        expected_chunk(f, 3, 10**6, 1 * MiB)
