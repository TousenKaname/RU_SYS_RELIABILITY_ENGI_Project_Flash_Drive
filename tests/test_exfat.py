"""The raw file-system checks, on an exFAT image and on the raw dumps of run 1's drives."""

from __future__ import annotations

from pathlib import Path

import pytest

from flashrel import exfat
from flashrel.platform.base import FileRaw

ID, SENT = "FLASHREL-ID.json", "FLASHREL-SENTINEL.bin"
RUN1 = Path(__file__).resolve().parent / "fixtures" / "run1_forensics"


def _flip(path: Path, offset: int, mask: int = 0x04) -> None:
    with open(path, "r+b") as fh:
        fh.seek(offset)
        b = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([b[0] ^ mask]))


def test_baseline_and_clean_check(exfat_image):
    with FileRaw(str(exfat_image)) as raw:
        base = exfat.baseline(raw, identity_name=ID, sentinel_name=SENT)
        result = exfat.check(raw, base)
    assert base["upcase"]["checksum"] == 0xE619D30D  # the table Windows writes as well
    assert set(base["files"]) == {ID, SENT}
    assert all(result[c]["ok"] for c in ("boot", "upcase", "rootdir"))


def test_volume_flags_do_not_trip_the_boot_checksum(exfat_image):
    with FileRaw(str(exfat_image)) as raw:
        base = exfat.baseline(raw, identity_name=ID, sentinel_name=SENT)
    _flip(exfat_image, 106, 0x02)  # VolumeDirty, excluded from the checksum by the spec
    _flip(exfat_image, 112, 0x10)  # PercentInUse, excluded as well
    with FileRaw(str(exfat_image)) as raw:
        result = exfat.check(raw, base)
    assert result["boot"]["ok"] and result["dirty"]


@pytest.mark.parametrize("where", ["boot", "upcase", "rootdir"])
def test_one_flipped_bit_is_found(exfat_image, where):
    with FileRaw(str(exfat_image)) as raw:
        base = exfat.baseline(raw, identity_name=ID, sentinel_name=SENT)
        boot = exfat.parse_boot(raw.read(0, 512))
    if where == "boot":
        offset = 300  # boot code, inside the checksummed region
    elif where == "upcase":
        offset = boot.cluster_offset(base["upcase"]["first_cluster"]) + 1000
    else:
        reader = exfat.VolumeReader(FileRaw(str(exfat_image)), boot)
        root = exfat.parse_directory(reader.chain(boot.root_cluster))
        entry = root.find(SENT)
        offset = boot.cluster_offset(boot.root_cluster) + entry.offset + 32 + 20  # its FirstCluster
    _flip(exfat_image, offset)
    with FileRaw(str(exfat_image)) as raw:
        result = exfat.check(raw, base)
    assert not result[where]["ok"]
    others = {"boot", "upcase", "rootdir"} - {where}
    assert all(result[c]["ok"] for c in others)


def _run1(drive: str):
    boot = exfat.parse_boot((RUN1 / f"{drive}_boot.bin").read_bytes())
    first = boot.root_cluster - 2  # dumps start at the bitmap cluster
    blob = (RUN1 / next(p.name for p in RUN1.glob(f"{drive}_first_clusters_*.bin"))).read_bytes()
    cl = boot.cluster_size
    return boot, (lambda k: blob[(k - first) * cl:(k - first + 1) * cl])


@pytest.mark.parametrize("drive", ["A8-01", "S8-01", "S8-02", "A16-01", "A16-02"])
def test_run1_intact_drives_pass(drive):
    boot, cluster = _run1(drive)
    root = exfat.parse_directory(cluster(boot.root_cluster))
    assert root.ok and root.find(ID) is not None
    up = root.first("upcase")
    assert exfat.checksum32(cluster(up.first_cluster)[: up.data_length]) == up.table_checksum


@pytest.mark.parametrize("drive", ["A8-02", "A8-03", "A8-04"])
def test_run1_damaged_drives_fail(drive):
    """The three ABLAZE 8 GB drives that lost their file-system records in run 1."""
    boot, cluster = _run1(drive)
    root = exfat.parse_directory(cluster(boot.root_cluster))
    assert not root.ok
    # the up-case table sits in cluster 3 on these volumes (as on A8-01)
    assert exfat.checksum32(cluster(3)[:5836]) != 0xE619D30D
