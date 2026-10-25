"""The post-test check of scripts/post_test_check.py on a simulated drive."""

from __future__ import annotations

import sys

from conftest import REPO

from flashrel.recorder import DriveLog
from flashrel.system.volumes import usage, write_identity

sys.path.insert(0, str(REPO / "scripts"))
import post_test_check  # noqa: E402


def test_post_test_check_passes_a_healthy_drive(campaign, drive, monkeypatch):
    identity = write_identity(drive, "T8-01", campaign=campaign.name)
    DriveLog(campaign.run_dir, "T8-01").save_intake(
        {"drive_id": "T8-01", "identity": identity, "total_bytes": usage(drive).total})
    monkeypatch.setattr(post_test_check, "find_drive", lambda drive_id: drive)
    record = post_test_check.check_one(campaign, "T8-01", "H")
    assert record["found"] and record["identity_intact"] and record["capacity_unchanged"]
    assert record["check"]["outcome"] == "pass" and record["passed"]
    events = DriveLog(campaign.run_dir, "T8-01").read_events()
    assert events[-1].detail["text"] == "post-test check" and events[-1].detail["passed"]


def test_post_test_check_reports_a_missing_drive(campaign, monkeypatch):
    monkeypatch.setattr(post_test_check, "find_drive", lambda drive_id: None)
    record = post_test_check.check_one(campaign, "T8-02", "H")
    assert record["found"] is False
