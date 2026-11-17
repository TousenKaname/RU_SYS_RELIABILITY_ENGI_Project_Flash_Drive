"""Host checks before a 5-day unattended run, and the optional fixes.

Run 1 lost control over several host settings because the shell was not elevated
(USB selective suspend, Windows Update, antivirus). Each check here reports ok / warn /
fail with a plain explanation; a fix is applied only when the operator clicks it, and
every change is logged with its previous value in host/host_changes.jsonl, so
"Undo host changes" can put the PC back as it was after the test.
"""

from __future__ import annotations

import os
import re
import shutil
import string
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flashrel.platform import Platform
from flashrel.store import CampaignStore, append_jsonl, read_jsonl
from flashrel.timeutil import iso

TASK_NAME = "FlashRel Rig resume"
USB_SUBGROUP = "2a737441-1930-4402-8d77-b2bebba308a3"
USB_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"
WU_KEY = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
AUTOPLAY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\AutoplayHandlers"
CREATE_NO_WINDOW = 0x08000000


def _run(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
    kw = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} did not finish within {timeout:.0f} s"
    except OSError as exc:
        return 127, str(exc)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    if sys.platform == "win32" and "\ufffd" in out:  # console code page output (localized Windows)
        out = (proc.stdout + proc.stderr).decode("mbcs", "replace")
    return proc.returncode, out


def _powercfg_value(subgroup: str, setting: str) -> int | None:
    """Current AC value of a power setting. powercfg prints localized text, so the value
    is taken as the second-to-last hex number (AC comes before DC on every language)."""
    code, out = _run(["powercfg", "/query", "SCHEME_CURRENT", subgroup, setting])
    values = re.findall(r"0x([0-9a-fA-F]{8})", out)
    if code != 0 or len(values) < 2:
        return None
    return int(values[-2], 16)


def _reg_get(root, path: str, name: str) -> Any:
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _reg_exists(root, path: str) -> bool:
    import winreg

    try:
        winreg.OpenKey(root, path).Close()
        return True
    except OSError:
        return False


def _ps(script: str, timeout: float = 90) -> Any:
    from flashrel.platform.windows import run_powershell

    return run_powershell(script, timeout=timeout)


def _check(cid: str, title: str, status: str, detail: str, fix: str = "") -> dict[str, Any]:
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix}


def run_checks(platform: Platform, package_dir: str, data_dir: str, n_drives: int = 20) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    admin = platform.is_admin()
    checks.append(_check("admin", "Administrator rights", "ok" if admin else "fail",
                         "formatting, device restart and host fixes are available" if admin else
                         "start the rig with 'Start FlashRel.bat' and accept the Windows prompt"))
    checks.append(_location_check(package_dir, data_dir))
    if sys.platform != "win32":
        checks.append(_check("platform", "Windows host", "info",
                             f"running on {platform.name}: host checks apply to the Windows rig only"))
        return checks
    import winreg

    from flashrel.platform import win32

    power = win32.power_status()
    checks.append(_check("ac_power", "On mains power", "ok" if power.get("ac_line") != "battery" else "fail",
                         f"power source: {power.get('ac_line', 'unknown')}"))
    standby = _powercfg_value("SUB_SLEEP", "STANDBYIDLE")
    hibernate = _powercfg_value("SUB_SLEEP", "HIBERNATEIDLE")
    sleep_ok = standby == 0 and hibernate in (0, None)
    checks.append(_check("sleep", "PC never sleeps on AC power", "ok" if sleep_ok else "fail",
                         f"sleep after {standby}s, hibernate after {hibernate}s (0 = never); the rig "
                         "also blocks sleep while it runs", "" if sleep_ok else "Set sleep/hibernate to never"))
    suspend = _powercfg_value(USB_SUBGROUP, USB_SUSPEND)
    checks.append(_check("usb_suspend", "USB selective suspend off", "ok" if suspend == 0 else "warn",
                         f"current value {suspend} (0 = disabled); suspend can power-cycle idle ports",
                         "" if suspend == 0 else "Disable USB selective suspend"))
    expiry = _reg_get(winreg.HKEY_LOCAL_MACHINE, WU_KEY, "PauseUpdatesExpiryTime")
    paused_until = _parse_time(expiry)
    need_until = datetime.now(timezone.utc) + timedelta(days=6)
    wu_ok = paused_until is not None and paused_until >= need_until
    checks.append(_check("windows_update", "Windows Update paused for the test",
                         "ok" if wu_ok else "warn",
                         f"paused until {paused_until.isoformat() if paused_until else 'not paused'}; an "
                         "update restart in the middle would interrupt every drive",
                         "" if wu_ok else "Pause updates for 10 days"))
    pending = (_reg_exists(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired")
               or _reg_exists(winreg.HKEY_LOCAL_MACHINE,
                              r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"))
    checks.append(_check("reboot_pending", "No restart pending", "fail" if pending else "ok",
                         "Windows wants to restart: restart the PC BEFORE the test starts" if pending
                         else "no restart pending"))
    autoplay = _reg_get(winreg.HKEY_CURRENT_USER, AUTOPLAY_KEY, "DisableAutoplay")
    checks.append(_check("autoplay", "AutoPlay off (no pop-up per drive)", "ok" if autoplay == 1 else "warn",
                         "AutoPlay opens a window for each of the 20 drives" if autoplay != 1 else "off",
                         "" if autoplay == 1 else "Turn AutoPlay off"))
    checks.append(_defender_check())
    checks.append(_letters_check(platform, n_drives))
    code, _ = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    checks.append(_check("auto_resume", "Resume automatically after a restart (optional)",
                         "ok" if code == 0 else "info",
                         "a Windows task restarts the rig when you log in after a restart" if code == 0
                         else "off: after an unexpected restart someone must start the rig again",
                         "" if code == 0 else "Enable auto-resume"))
    return checks


def _parse_time(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _location_check(package_dir: str, data_dir: str) -> dict[str, Any]:
    path = os.path.abspath(data_dir)
    parts = [p.lower() for p in Path(path).parts]
    synced = [p for p in parts if p in ("onedrive", "dropbox", "google drive", "my drive", "icloud drive",
                                         "icloudrive", "box", "我的云端硬盘", "icloud云盘", "百度网盘")
              or p.startswith("onedrive - ") or p.startswith("onedrive-")]
    free = shutil.disk_usage(path).free if os.path.exists(path) else 0
    problems = []
    if synced:
        problems.append(f"inside a sync folder ({synced[0]}): sync clients lock files; copy the rig to C:\\FlashRel-Rig")
    if free < 2 * 1024**3:
        problems.append(f"only {free / 1e9:.1f} GB free on the data disk")
    if sys.platform == "win32":
        from flashrel.platform import win32

        root = os.path.splitdrive(path)[0] + "\\"
        if win32.drive_type(root) != win32.DRIVE_FIXED:
            problems.append("the rig runs from a removable or network drive: copy it to the PC's own disk")
    return _check("location", "Rig folder on the PC's own disk", "fail" if problems else "ok",
                  "; ".join(problems) if problems else f"{path} ({free / 1e9:.0f} GB free)")


def _defender_check() -> dict[str, Any]:
    try:
        info = _ps("$s = Get-MpComputerStatus; $p = Get-MpPreference; "
                   "ConvertTo-Json -Compress -InputObject ([ordered]@{rt=[bool]$s.RealTimeProtectionEnabled; "
                   "mode=[string]$s.AMRunningMode; excl=@($p.ExclusionProcess)})")
    except Exception as exc:
        return _check("defender", "Antivirus does not scan the test I/O", "info",
                      f"Windows Defender status unavailable ({str(exc)[:80]}); if another antivirus "
                      "is installed, exclude the test drives from its real-time scan")
    exe = os.path.abspath(sys.executable).lower()
    excluded = any(str(e).lower() == exe or str(e).lower().endswith("python.exe") and
                   os.path.dirname(str(e)).lower() == os.path.dirname(exe) for e in info.get("excl") or [])
    if not info.get("rt") or "passive" in str(info.get("mode", "")).lower():
        return _check("defender", "Antivirus does not scan the test I/O", "info",
                      f"Defender real-time protection off or passive ({info.get('mode')}); another "
                      "antivirus may be active: exclude the test drives in it if you can")
    return _check("defender", "Antivirus does not scan the test I/O", "ok" if excluded else "warn",
                  "the rig's Python is excluded from Defender scanning" if excluded else
                  "Defender scans every file the rig writes (slows small files, can cause access errors)",
                  "" if excluded else "Exclude the rig's Python from Defender")


def _letters_check(platform: Platform, n_drives: int) -> dict[str, Any]:
    from flashrel.platform import win32

    used = {v.letter for v in platform.list_volumes() if v.letter}
    mask = win32.k32.GetLogicalDrives()
    taken = {letter for i, letter in enumerate(string.ascii_uppercase) if mask >> i & 1}
    free = [letter for letter in string.ascii_uppercase[3:] if letter not in taken]
    test_mounted = len(used)
    needed = max(0, n_drives - test_mounted)
    status = "ok" if len(free) >= needed else "warn"
    return _check("letters", "Enough drive letters", status,
                  f"{len(free)} letters free, about {needed} more needed; drives without a letter still "
                  "work (the rig uses volume paths), but Explorer will not show them")


# -- fixes ----------------------------------------------------------------------------------
def apply_fix(cstore: CampaignStore, check_id: str) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": False, "detail": "host fixes apply to the Windows rig only"}
    import winreg

    log = cstore.host_dir / "host_changes.jsonl"
    if check_id == "sleep":
        before = {"standby": _powercfg_value("SUB_SLEEP", "STANDBYIDLE"),
                  "hibernate": _powercfg_value("SUB_SLEEP", "HIBERNATEIDLE")}
        r1 = _run(["powercfg", "/change", "standby-timeout-ac", "0"])
        r2 = _run(["powercfg", "/change", "hibernate-timeout-ac", "0"])
        ok = r1[0] == 0 and r2[0] == 0
    elif check_id == "usb_suspend":
        before = {"usb_suspend": _powercfg_value(USB_SUBGROUP, USB_SUSPEND)}
        r1 = _run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", USB_SUBGROUP, USB_SUSPEND, "0"])
        r2 = _run(["powercfg", "/setactive", "SCHEME_CURRENT"])
        ok = r1[0] == 0 and r2[0] == 0
    elif check_id == "windows_update":
        before = {name: _reg_get(winreg.HKEY_LOCAL_MACHINE, WU_KEY, name)
                  for name in ("PauseUpdatesExpiryTime", "PauseFeatureUpdatesStartTime",
                               "PauseFeatureUpdatesEndTime", "PauseQualityUpdatesStartTime",
                               "PauseQualityUpdatesEndTime", "PauseUpdatesStartTime")}
        now = datetime.now(timezone.utc)
        start, end = now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, WU_KEY, 0, winreg.KEY_SET_VALUE) as key:
                for name, value in (("PauseUpdatesStartTime", start), ("PauseUpdatesExpiryTime", end),
                                    ("PauseFeatureUpdatesStartTime", start), ("PauseFeatureUpdatesEndTime", end),
                                    ("PauseQualityUpdatesStartTime", start), ("PauseQualityUpdatesEndTime", end)):
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            ok = True
        except OSError as exc:
            append_jsonl(log, {"time": iso(), "fix": check_id, "ok": False, "error": str(exc)})
            return {"ok": False, "detail": str(exc)}
    elif check_id == "autoplay":
        before = {"DisableAutoplay": _reg_get(winreg.HKEY_CURRENT_USER, AUTOPLAY_KEY, "DisableAutoplay")}
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOPLAY_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "DisableAutoplay", 0, winreg.REG_DWORD, 1)
        ok = True
    elif check_id == "defender":
        exe = os.path.abspath(sys.executable)
        exes = sorted({exe, os.path.join(os.path.dirname(exe), "python.exe"),
                       os.path.join(os.path.dirname(exe), "pythonw.exe")})
        before = {"added": exes}
        quoted = ",".join("'" + e.replace("'", "''") + "'" for e in exes)
        try:
            _ps(f"Add-MpPreference -ExclusionProcess @({quoted}); ConvertTo-Json -Compress -InputObject @{{ok=$true}}")
            ok = True
        except Exception as exc:
            append_jsonl(log, {"time": iso(), "fix": check_id, "ok": False, "error": str(exc)[:300]})
            return {"ok": False, "detail": str(exc)[:300]}
    elif check_id == "auto_resume":
        bat = Path(os.environ.get("FLASHREL_LAUNCHER", "")).resolve()
        if not bat.is_file():
            return {"ok": False, "detail": "start the rig with 'Start FlashRel.bat' to enable auto-resume"}
        before = {"task": None}
        user = os.environ.get("USERNAME", "")
        cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "HIGHEST", "/F",
               "/TR", f'"{bat}" --resume']
        if user:
            cmd += ["/RU", user, "/IT"]  # interactive only: no password is asked or stored
        code, out = _run(cmd)
        ok = code == 0
        if not ok:
            return {"ok": False, "detail": out.strip()[:300]}
    else:
        return {"ok": False, "detail": f"no fix for {check_id}"}
    append_jsonl(log, {"time": iso(), "fix": check_id, "ok": ok, "before": before})
    cstore.operator("host_fix", check=check_id, ok=ok)
    return {"ok": ok, "detail": "applied" if ok else "failed"}


def undo_all(cstore: CampaignStore) -> list[dict[str, Any]]:
    """Put back every host setting the rig changed (newest first)."""
    if sys.platform != "win32":
        return []
    import winreg

    done = []
    for change in reversed(read_jsonl(cstore.host_dir / "host_changes.jsonl")):
        if not change.get("ok") or change.get("undone") or change.get("undo"):
            continue
        fix, before = change.get("fix"), change.get("before") or {}
        try:
            if fix == "sleep":
                if before.get("standby") is not None:
                    _run(["powercfg", "/change", "standby-timeout-ac", str(before["standby"] // 60)])
                if before.get("hibernate") is not None:
                    _run(["powercfg", "/change", "hibernate-timeout-ac", str(before["hibernate"] // 60)])
            elif fix == "usb_suspend" and before.get("usb_suspend") is not None:
                _run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", USB_SUBGROUP, USB_SUSPEND,
                      str(before["usb_suspend"])])
                _run(["powercfg", "/setactive", "SCHEME_CURRENT"])
            elif fix == "windows_update":
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, WU_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    for name, value in before.items():
                        if value is None:
                            try:
                                winreg.DeleteValue(key, name)
                            except OSError:
                                pass
                        else:
                            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            elif fix == "autoplay":
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOPLAY_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, "DisableAutoplay", 0, winreg.REG_DWORD, int(before.get("DisableAutoplay") or 0))
            elif fix == "defender":
                quoted = ",".join("'" + e.replace("'", "''") + "'" for e in before.get("added", []))
                _ps(f"Remove-MpPreference -ExclusionProcess @({quoted}); ConvertTo-Json -Compress -InputObject @{{ok=$true}}")
            elif fix == "auto_resume":
                _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
            done.append({"fix": fix, "ok": True})
        except Exception as exc:  # report and continue with the others
            done.append({"fix": fix, "ok": False, "error": str(exc)[:200]})
    append_jsonl(cstore.host_dir / "host_changes.jsonl", {"time": iso(), "undo": done})
    cstore.operator("host_undo", results=done)
    return done
