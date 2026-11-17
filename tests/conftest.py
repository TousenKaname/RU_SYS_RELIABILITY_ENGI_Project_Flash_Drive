from __future__ import annotations

import gzip
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flashrel.config import for_simulation, load_campaign  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG = ROOT / "config" / "campaign.toml"


@pytest.fixture
def campaign():
    return load_campaign(CONFIG)


@pytest.fixture
def sim_campaign():
    return for_simulation(load_campaign(CONFIG), duration_h=0.01, speed=1000)


@pytest.fixture
def exfat_image(tmp_path) -> Path:
    """An 8 MiB exFAT volume (made with macOS newfs_exfat) holding an identity file, a
    64 KiB sentinel and one folder of payload files."""
    target = tmp_path / "exfat.img"
    with gzip.open(FIXTURES / "exfat_fixture.img.gz", "rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return target
