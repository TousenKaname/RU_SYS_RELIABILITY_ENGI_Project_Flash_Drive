"""Deterministic test payloads.

The harness keeps no source files on the host. Every file it writes to a drive
is generated from a 64-bit key, so the expected bytes can be regenerated at any
time, both for byte-exact verification and for diagnosing a corrupted chunk.
This also keeps the host's own disk out of the data path: a mismatch can only
come from the drive, the USB link, or host memory, never from a corrupted
source file.

Every chunk (1 MiB by default) begins with a 32-byte header

    offset  size  field
         0     6  magic b"FLSHRL"
         6     2  format version
         8     8  file key
        16     4  cycle number
        20     4  file index within the cycle
        24     8  chunk index within the file

followed by the SHAKE-128 output stream of (file key, chunk index), which is
bit-exact on every platform and Python version. The header makes corrupted
chunks traceable: stale data from an earlier cycle, data that belongs to
another file, and erased pages each leave a distinct signature.
"""

from __future__ import annotations

import hashlib
import math
import random
import struct
from collections.abc import Iterator
from dataclasses import dataclass

from flashrel.config import WorkloadSpec

HEADER = struct.Struct("<6sHQIIQ")
MAGIC = b"FLSHRL"
FORMAT_VERSION = 1
PAYLOAD_DIR = "FLASHREL-DATA"


def stable_key(*parts: object) -> int:
    """A 64-bit key that is identical on every machine and Python version."""
    text = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(text, digest_size=8).digest(), "little")


@dataclass(frozen=True)
class FileSpec:
    """One payload file: where it goes, how long it is, and its content key."""

    index: int
    relpath: str
    size: int
    key: int


@dataclass(frozen=True)
class CyclePlan:
    """The complete, reproducible file set of one cycle on one drive."""

    drive_id: str
    cycle: int
    workload: str
    chunk_size: int
    files: tuple[FileSpec, ...]

    @property
    def dirname(self) -> str:
        return f"c{self.cycle:06d}"

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


def _round_up(n: int, unit: int) -> int:
    return -(-n // unit) * unit


def fill_budget(free_bytes: int, fill_fraction: float, reserve_bytes: int) -> int:
    """Bytes one cycle may allocate: a fraction of the free space, never the reserve."""
    return max(0, min(int(free_bytes * fill_fraction), free_bytes - reserve_bytes))


def plan_cycle(
    *,
    seed: int,
    drive_id: str,
    cycle: int,
    workload: WorkloadSpec,
    budget_bytes: int,
    cluster_size: int,
    chunk_size: int,
) -> CyclePlan:
    """Draw file sizes until the allocated space reaches ``budget_bytes``.

    Sizes are log-uniform in [min_size, max_size], so each size decade gets the
    same number of files. Allocation is counted in whole clusters, because a
    4 KiB file still occupies a full 32 KiB exFAT cluster. A final, shorter file
    uses up the remaining budget so that every cycle fills the drive to the
    same level.
    """
    if cluster_size <= 0:
        raise ValueError("cluster_size must be positive")
    rng = random.Random(stable_key(seed, drive_id, cycle, "sizes"))
    lo, hi = math.log(workload.min_size), math.log(workload.max_size)
    sizes: list[int] = []
    used = 0
    while True:
        size = int(math.exp(rng.uniform(lo, hi)))
        need = _round_up(size, cluster_size)
        if used + need <= budget_bytes:
            sizes.append(size)
            used += need
            continue
        last = (budget_bytes - used) // cluster_size * cluster_size
        if last >= workload.min_size:
            sizes.append(last)
        break
    files = tuple(
        FileSpec(
            index=i,
            relpath=f"d{i // workload.files_per_dir:04d}/f{i:06d}.bin",
            size=size,
            key=stable_key(seed, drive_id, cycle, i),
        )
        for i, size in enumerate(sizes)
    )
    return CyclePlan(drive_id, cycle, workload.name, chunk_size, files)


def chunk_count(size: int, chunk_size: int) -> int:
    return -(-size // chunk_size)


def expected_chunk(spec: FileSpec, cycle: int, chunk_index: int, chunk_size: int) -> bytes:
    """Regenerate chunk ``chunk_index`` of a payload file."""
    length = min(chunk_size, spec.size - chunk_index * chunk_size)
    if length <= 0 or chunk_index < 0:
        raise IndexError(f"chunk {chunk_index} is outside a {spec.size}-byte file")
    header = HEADER.pack(MAGIC, FORMAT_VERSION, spec.key, cycle, spec.index, chunk_index)
    if length <= HEADER.size:
        return header[:length]
    seed = spec.key.to_bytes(8, "little") + chunk_index.to_bytes(8, "little")
    return header + hashlib.shake_128(seed).digest(length - HEADER.size)


def iter_expected(spec: FileSpec, cycle: int, chunk_size: int) -> Iterator[bytes]:
    for i in range(chunk_count(spec.size, chunk_size)):
        yield expected_chunk(spec, cycle, i, chunk_size)
