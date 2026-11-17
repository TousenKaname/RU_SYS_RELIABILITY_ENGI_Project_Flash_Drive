"""Command line: ``python -m flashrel <command>``.

    app        the rig: controller + dashboard (what "Start FlashRel.bat" runs)
    worker     one drive's worker (started by the controller, not by hand)
    preflight  quick self-check of the installation (run by the launcher)
    status     print the state of every drive of a campaign
    export     write the life table, the workbook and a ZIP of the data folder
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _package_dir() -> Path:
    """The folder of the packaged rig (app/ sits inside it), or the source checkout."""
    here = Path(__file__).resolve().parent  # .../app/flashrel or .../src/flashrel
    return here.parent.parent


def _defaults() -> dict[str, str]:
    root = _package_dir()
    return {"config": str(root / "config" / "campaign.toml"), "data": str(root / "data"),
            "exports": str(root / "exports")}


def _redirect_output(log_path: Path) -> None:
    """pythonw.exe has no console: send output to the controller log."""
    if sys.stdout is None or sys.stderr is None or os.environ.get("FLASHREL_LOG_TO_FILE"):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = fh
        sys.stderr = fh


def cmd_app(args: argparse.Namespace) -> int:
    from flashrel.controller import AlreadyRunning, Controller
    from flashrel.platform import get_platform
    from flashrel.server import serve

    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    sim_root = None
    if args.sim:
        sim_root = str(Path(args.sim_root or (data / "_sim")).resolve())
    _redirect_output(data / "controller.log")
    platform = get_platform(sim_root, args.sim_speed)
    try:
        ctrl = Controller(config_path=args.config, data_dir=str(data), exports_dir=args.exports,
                          platform=platform, package_dir=str(_package_dir()), sim_root=sim_root,
                          sim_speed=args.sim_speed, sim_hours=args.sim_hours)
    except AlreadyRunning as exc:
        print(f"FlashRel is already running: {exc.url}", flush=True)
        try:
            (data / "dashboard_url.txt").write_text(exc.url + "\n", encoding="ascii")
        except OSError:
            pass
        if not args.no_browser:
            platform.open_url(exc.url)
        return 0
    httpd, url = serve(ctrl, args.port)
    print(f"FlashRel Rig {ctrl.campaign.name} on {platform.name}: {url}", flush=True)
    try:  # the launcher waits for this file to confirm the start
        (data / "dashboard_url.txt").write_text(url + "\n", encoding="ascii")
    except OSError:
        pass
    import signal

    for sig in ("SIGTERM", "SIGBREAK"):  # stop the workers cleanly, as the dashboard button does
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: ctrl.shutdown_requested.set())
    ctrl.start_background()
    if not args.no_browser:
        platform.open_url(url)
    try:
        ctrl.loop_forever()
    except KeyboardInterrupt:
        print("stopping (Ctrl+C): workers end after the current file ...", flush=True)
        ctrl.shutdown_requested.set()
    finally:
        httpd.shutdown()
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """Checks the launcher runs before starting the rig in the background."""
    problems = []
    try:
        import ctypes  # noqa: F401
        import tomllib  # noqa: F401

        from flashrel import controller, server  # noqa: F401
        from flashrel.config import load_campaign
        from flashrel.platform import get_platform

        campaign = load_campaign(args.config)
        platform = get_platform()
        print(f"campaign {campaign.name}: {len(campaign.test_drives())} drives, "
              f"{campaign.duration_h:g} h; platform {platform.name}; admin {platform.is_admin()}")
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
    data = Path(args.data)
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        problems.append(f"cannot write to {data}: {exc}")
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


def cmd_status(args: argparse.Namespace) -> int:
    from flashrel.config import load_campaign
    from flashrel.store import CampaignStore

    campaign = load_campaign(args.config)
    cstore = CampaignStore(args.data, campaign.name)
    print(f"{'drive':8} {'hub':4} {'status':11} {'cycles':>6} {'fail':>4} {'first failure':16} {'FDW':>7}")
    for unit in campaign.test_drives():
        ds = cstore.drive(unit)
        st = ds.load_state() or {}
        rec = ds.load_intake() or {}
        slot = campaign.slot_of(unit)
        first = st.get("first_failure") or {}
        usable = int(rec.get("usable_bytes") or 0)
        fdw = (st.get("cum") or {}).get("bytes_written", 0) / usable if usable else 0
        ff = f"c{first['cycle']} {first['code']}" if first else ""
        print(f"{unit:8} {slot.label if slot else '':4} {st.get('status', 'not enrolled'):11} "
              f"{st.get('cycles_done', 0):>6} {len(st.get('failure_cycles') or []):>4} {ff:16} {fdw:7.2f}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from flashrel.config import Campaign, load_campaign
    from flashrel.export import export_zip
    from flashrel.store import CampaignStore, read_json

    campaign = load_campaign(args.config)
    cstore = CampaignStore(args.data, campaign.name)
    frozen = read_json(cstore.campaign_json)
    if frozen:
        campaign = Campaign.from_json(frozen)
    result = export_zip(campaign, cstore, args.exports)
    print(f"wrote {result['zip']} ({result['files']} files, {result['size'] / 1e6:.1f} MB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "worker":
        from flashrel.worker import worker_main

        return worker_main(argv[1:])
    d = _defaults()
    p = argparse.ArgumentParser(prog="flashrel", description="FlashRel Rig 2 - USB flash drive life test")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("app", "preflight", "status", "export"):
        s = sub.add_parser(name)
        s.add_argument("--config", default=d["config"])
        s.add_argument("--data", default=d["data"])
        s.add_argument("--exports", default=d["exports"])
        if name == "app":
            s.add_argument("--port", type=int, default=8585)
            s.add_argument("--no-browser", action="store_true")
            s.add_argument("--resume", action="store_true", help="(used by the auto-resume task)")
            s.add_argument("--sim", action="store_true", help="simulated drives (demo / development)")
            s.add_argument("--sim-root")
            s.add_argument("--sim-speed", type=float, default=20.0)
            s.add_argument("--sim-hours", type=float, default=0.5)
    args = p.parse_args(argv)
    # (--resume needs no delay here: the controller itself waits 20 s before it restarts the
    # workers, and each worker waits up to 2 min for its stick to appear.)
    return {"app": cmd_app, "preflight": cmd_preflight, "status": cmd_status, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
