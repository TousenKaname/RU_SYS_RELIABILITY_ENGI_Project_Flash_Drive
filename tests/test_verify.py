from __future__ import annotations

from flashrel.payload import FileSpec, expected_chunk
from flashrel.system.directio import read_chunks, write_file
from flashrel.verify import CorruptionKind, FileStatus, diagnose_chunk, verify_file

CHUNK = 64 * 1024
SPEC = FileSpec(index=5, relpath="d0000/f000005.bin", size=3 * CHUNK + 1000, key=0xABCDEF)
OTHER = FileSpec(index=6, relpath="d0000/f000006.bin", size=CHUNK, key=0x123456)


def diag(observed: bytes, chunk_index: int = 1, cycle: int = 9):
    expected = expected_chunk(SPEC, cycle, chunk_index, CHUNK)
    return diagnose_chunk(expected, observed, chunk_index=chunk_index, cycle=cycle, spec=SPEC)


def test_identical_chunk_has_no_diff():
    assert diag(expected_chunk(SPEC, 9, 1, CHUNK)) is None


def test_sparse_bit_flips():
    data = bytearray(expected_chunk(SPEC, 9, 1, CHUNK))
    data[500] ^= 0x01
    data[9000] ^= 0x80
    d = diag(bytes(data))
    assert (d.kind, d.bytes_diff, d.bits_diff) == (CorruptionKind.BIT_ERRORS, 2, 2)


def test_stale_chunk_from_earlier_cycle():
    assert diag(expected_chunk(SPEC, 8, 1, CHUNK)).kind is CorruptionKind.STALE


def test_misdirected_chunk_from_other_file():
    assert diag(expected_chunk(OTHER, 9, 0, CHUNK)).kind is CorruptionKind.MISDIRECTED


def test_erased_page():
    assert diag(b"\xff" * CHUNK).kind is CorruptionKind.ERASED
    assert diag(b"\x00" * CHUNK).kind is CorruptionKind.ERASED


def test_garbled_and_truncated():
    assert diag(bytes(range(256)) * (CHUNK // 256)).kind is CorruptionKind.GARBLED
    half = expected_chunk(SPEC, 9, 1, CHUNK)[: CHUNK // 2]
    d = diag(half)
    assert (d.kind, d.bytes_diff) == (CorruptionKind.TRUNCATED, CHUNK // 2)


def _write(path, spec, cycle=9):
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = (expected_chunk(spec, cycle, i, CHUNK) for i in range(-(-spec.size // CHUNK)))
    write_file(path, chunks, sync=True)


def _verify(path, uncached=True):
    return verify_file(path, SPEC, cycle=9, chunk_size=CHUNK,
                       read_chunks=lambda p, n: read_chunks(p, n, uncached=uncached))


def test_verify_file_ok_with_cache_bypass(tmp_path):
    path = tmp_path / SPEC.relpath
    _write(path, SPEC)
    for uncached in (True, False):
        check = _verify(path, uncached)
        assert check.status is FileStatus.OK and check.observed_size == SPEC.size


def test_verify_file_detects_missing_truncated_and_flipped(tmp_path):
    path = tmp_path / SPEC.relpath
    assert _verify(path).status is FileStatus.MISSING
    _write(path, SPEC)
    raw = bytearray(path.read_bytes())
    raw[CHUNK + 100] ^= 0x04
    path.write_bytes(bytes(raw[: 2 * CHUNK + 10]))  # one flip, then cut the file short
    check = _verify(path)
    kinds = [d.kind for d in check.diffs]
    assert check.status is FileStatus.CORRUPT
    assert kinds == [CorruptionKind.BIT_ERRORS, CorruptionKind.TRUNCATED, CorruptionKind.TRUNCATED]
    assert check.observed_size == 2 * CHUNK + 10
