"""Byte-exact verification of payload files and diagnosis of corrupted chunks.

Verification regenerates the expected stream (see ``payload``) and compares it
with what the drive returns, chunk by chunk. A byte-exact comparison is
stronger than a checksum and, unlike a checksum, says *how* a file is wrong.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from flashrel.payload import HEADER, MAGIC, FileSpec, chunk_count, expected_chunk

#: Above this fraction of flipped bits a chunk is called "garbled", not "bit errors".
SPARSE_BIT_FRACTION = 0.01


class CorruptionKind(str, Enum):
    BIT_ERRORS = "bit_errors"    # header intact or nearly so, sparse bit flips
    GARBLED = "garbled"          # dense differences with no recognisable header
    STALE = "stale"              # a chunk written in an earlier cycle (old mapping)
    MISDIRECTED = "misdirected"  # a chunk of another file or offset in this cycle
    ERASED = "erased"            # all 0x00 or all 0xFF: unwritten or erased page
    TRUNCATED = "truncated"      # bytes missing at the end of the file
    EXTRA = "extra"              # bytes beyond the expected end of the file


class FileStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class ChunkDiff:
    chunk_index: int
    kind: CorruptionKind
    bytes_diff: int
    bits_diff: int


@dataclass
class FileCheck:
    relpath: str
    status: FileStatus
    expected_size: int
    observed_size: int | None
    diffs: list[ChunkDiff] = field(default_factory=list)

    @property
    def bytes_diff(self) -> int:
        return sum(d.bytes_diff for d in self.diffs)

    @property
    def bits_diff(self) -> int:
        return sum(d.bits_diff for d in self.diffs)


def _bit_flips(a: bytes, b: bytes) -> int:
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).bit_count()


def diagnose_chunk(expected: bytes, observed: bytes, *, chunk_index: int, cycle: int,
                   spec: FileSpec) -> ChunkDiff | None:
    """Classify how ``observed`` differs from ``expected``; ``None`` if identical."""
    if observed == expected:
        return None
    n = min(len(expected), len(observed))
    if len(observed) < len(expected) and observed == expected[:n]:
        missing = len(expected) - n
        return ChunkDiff(chunk_index, CorruptionKind.TRUNCATED, missing, 8 * missing)
    head_e, head_o = expected[:n], observed[:n]
    bits = _bit_flips(head_e, head_o)
    nbytes = sum(1 for x, y in zip(head_e, head_o) if x != y) + abs(len(expected) - len(observed))
    if observed and observed.count(observed[:1]) == len(observed) and observed[0] in (0x00, 0xFF):
        kind = CorruptionKind.ERASED
    elif len(observed) >= HEADER.size and observed[:6] == MAGIC:
        _, _, key, cyc, _, chunk = HEADER.unpack_from(observed)
        if key == spec.key and chunk == chunk_index and cyc == cycle:
            kind = CorruptionKind.BIT_ERRORS
        elif cyc < cycle:
            kind = CorruptionKind.STALE
        else:
            kind = CorruptionKind.MISDIRECTED
    elif n and bits <= SPARSE_BIT_FRACTION * 8 * n:
        kind = CorruptionKind.BIT_ERRORS
    else:
        kind = CorruptionKind.GARBLED
    return ChunkDiff(chunk_index, kind, nbytes, bits)


ChunkSource = Callable[[Path, int], Iterable[bytes | memoryview]]


def verify_file(path: Path, spec: FileSpec, *, cycle: int, chunk_size: int,
                read_chunks: ChunkSource) -> FileCheck:
    """Compare one file on the drive with its regenerated reference stream.

    ``read_chunks(path, chunk_size)`` yields the file in ``chunk_size`` pieces
    (the last one may be shorter). OS errors propagate to the caller, which
    decides whether to retry, so a read error is never mistaken for corruption.
    """
    try:
        observed_size = os.stat(path).st_size
    except FileNotFoundError:
        return FileCheck(spec.relpath, FileStatus.MISSING, spec.size, None)
    check = FileCheck(spec.relpath, FileStatus.OK, spec.size, observed_size)
    n_expected = chunk_count(spec.size, chunk_size)
    index = -1
    for index, chunk in enumerate(read_chunks(path, chunk_size)):
        if index >= n_expected:
            check.diffs.append(ChunkDiff(index, CorruptionKind.EXTRA, len(chunk), 8 * len(chunk)))
            continue
        expected = expected_chunk(spec, cycle, index, chunk_size)
        if chunk != expected:
            diff = diagnose_chunk(expected, bytes(chunk), chunk_index=index, cycle=cycle, spec=spec)
            if diff is not None:
                check.diffs.append(diff)
    for missing in range(index + 1, n_expected):
        expected_len = len(expected_chunk(spec, cycle, missing, chunk_size))
        check.diffs.append(ChunkDiff(missing, CorruptionKind.TRUNCATED, expected_len,
                                     8 * expected_len))
    if check.diffs:
        check.status = FileStatus.CORRUPT
    return check
