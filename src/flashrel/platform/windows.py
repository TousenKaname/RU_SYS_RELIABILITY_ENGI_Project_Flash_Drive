"""The real rig: Windows 10/11, USB drives behind two hubs.

Fast queries (volumes, free space, bus type, serial number) use kernel32 through
ctypes. Slow, rarely needed facts (USB location path of each drive, its parent hub,
the removal policy Windows gave it) and the administrator actions (format, device
restart) go through Windows PowerShell 5.1, which every Windows 10/11 PC has.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from typing import Any

from flashrel.platform import win32
from flashrel.usbpath import location_key  # noqa: F401  (re-exported)
from flashrel.platform.base import Platform, RawVolume, StorageInfo, Unsupported, Volume

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
POWERSHELL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                          "WindowsPowerShell", "v1.0", "powershell.exe")
PS_PRELUDE = (
    "$ProgressPreference = 'SilentlyContinue'\n"
    "$ErrorActionPreference = 'Stop'\n"
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
)


def run_powershell(script: str, timeout: float = 90.0) -> Any:
    """Run a script; it must print one JSON document. Returns the parsed document.

    Every failure (cannot start, timeout, no or bad JSON) is raised as RuntimeError, so
    callers handle one exception type."""
    encoded = base64.b64encode((PS_PRELUDE + script).encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-EncodedCommand", encoded], capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"PowerShell did not finish within {timeout:.0f} s") from None
    except OSError as exc:
        raise RuntimeError(f"PowerShell could not start: {exc}") from None
    out = proc.stdout.decode("utf-8", "replace").lstrip("\ufeff").strip()
    if not out:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"PowerShell returned nothing (exit {proc.returncode}): {err[:500]}")
    # A script may print warnings before its JSON; the document is the last line.
    try:
        return json.loads(out.splitlines()[-1])
    except ValueError:
        raise RuntimeError(f"PowerShell output is not JSON: {out[-300:]}") from None


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# One call collects every USB disk: number, serial, size, volumes, PnP instance IDs,
# USB location paths (which hub port it sits on), and the removal policy Windows uses.
INVENTORY_PS = r"""
$ErrorActionPreference = 'Continue'
function Get-Props($id, [string[]]$keys) {
  $h = @{}
  try {
    foreach ($p in @(Get-PnpDeviceProperty -InstanceId $id -KeyName $keys -ErrorAction Stop)) {
      $h[$p.KeyName] = $p.Data
    }
  } catch {}
  return $h
}
$result = New-Object System.Collections.ArrayList
foreach ($d in @(Get-Disk | Where-Object { "$($_.BusType)" -eq 'USB' })) {
  $diskId = $null
  if ("$($d.Path)" -match '^\\\\\?\\(.+)#\{[0-9a-fA-F-]+\}$') { $diskId = ($Matches[1] -replace '#', '\') }
  $dp = @{}; $up = @{}
  if ($diskId) { $dp = Get-Props $diskId @('DEVPKEY_Device_RemovalPolicy', 'DEVPKEY_Device_Parent', 'DEVPKEY_Device_InstanceId') }
  $usbId = $dp['DEVPKEY_Device_Parent']
  if ($usbId) { $up = Get-Props $usbId @('DEVPKEY_Device_LocationPaths', 'DEVPKEY_Device_LocationInfo', 'DEVPKEY_Device_Parent', 'DEVPKEY_Device_BusReportedDeviceDesc') }
  $hubName = $null
  if ($up['DEVPKEY_Device_Parent']) { try { $hubName = (Get-PnpDevice -InstanceId $up['DEVPKEY_Device_Parent'] -ErrorAction Stop).FriendlyName } catch {} }
  $vols = New-Object System.Collections.ArrayList
  try {
    foreach ($p in @(Get-Partition -DiskNumber $d.Number -ErrorAction Stop)) {
      $v = $null
      try { $v = $p | Get-Volume -ErrorAction Stop } catch {}
      $letter = [string]$p.DriveLetter   # [char]0 when there is none; Python strips it
      [void]$vols.Add([ordered]@{
        partition = $p.PartitionNumber; offset = $p.Offset; size = $p.Size; letter = $letter
        access_paths = @($p.AccessPaths)
        fs = $(if ($v) { [string]$v.FileSystem } else { '' })
        label = $(if ($v) { [string]$v.FileSystemLabel } else { '' })
        free = $(if ($v) { $v.SizeRemaining } else { $null })
        health = $(if ($v) { [string]$v.HealthStatus } else { '' })
        guid = $(if ($v) { [string]$v.Path } else { '' })
      })
    }
  } catch {}
  [void]$result.Add([ordered]@{
    number = $d.Number; serial = [string]$d.SerialNumber; model = [string]$d.FriendlyName
    size = $d.Size; boot = [bool]$d.IsBoot; system = [bool]$d.IsSystem
    readonly = [bool]$d.IsReadOnly; offline = [bool]$d.IsOffline
    health = [string]$d.HealthStatus; operational = [string]$d.OperationalStatus
    disk_instance = $diskId; usb_instance = $usbId
    hub_instance = $up['DEVPKEY_Device_Parent']; hub_name = $hubName
    location_info = $up['DEVPKEY_Device_LocationInfo']
    location_paths = @($up['DEVPKEY_Device_LocationPaths'])
    bus_desc = $up['DEVPKEY_Device_BusReportedDeviceDesc']
    removal_policy = $dp['DEVPKEY_Device_RemovalPolicy']
    volumes = @($vols)
  })
}
ConvertTo-Json -InputObject @($result) -Depth 6 -Compress
"""

FORMAT_PS = r"""
try {
  $target = '__TARGET__'
  $v = $null
  if ($target.Length -eq 1) { $v = Get-Volume -DriveLetter $target }
  else { $v = Get-Volume | Where-Object { $_.Path -eq $target -or $_.UniqueId -eq $target } | Select-Object -First 1 }
  if (-not $v) { throw "volume $target not found" }
  $r = $v | Format-Volume -FileSystem exFAT -AllocationUnitSize 32768 -NewFileSystemLabel '__LABEL__' -Force -Confirm:$false
  ConvertTo-Json -Compress -InputObject ([ordered]@{ ok = $true; fs = [string]$r.FileSystem; label = [string]$r.FileSystemLabel; size = $r.Size; free = $r.SizeRemaining })
} catch {
  ConvertTo-Json -Compress -InputObject ([ordered]@{ ok = $false; error = $_.Exception.Message })
}
"""

HOSTINFO_PS = r"""
$cs = $null; $os = $null; $cpu = $null
try { $cs = Get-CimInstance Win32_ComputerSystem } catch {}
try { $os = Get-CimInstance Win32_OperatingSystem } catch {}
try { $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 } catch {}
$ctrl = @()
try { $ctrl = @(Get-PnpDevice -Class USB -PresentOnly | Where-Object { $_.InstanceId -like 'PCI\*' } | ForEach-Object { $_.FriendlyName }) } catch {}
$av = @()
try { $av = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | ForEach-Object { $_.displayName }) } catch {}
ConvertTo-Json -Compress -InputObject ([ordered]@{
  manufacturer = $(if ($cs) { [string]$cs.Manufacturer } else { '' })
  model = $(if ($cs) { [string]$cs.Model } else { '' })
  ram_bytes = $(if ($cs) { $cs.TotalPhysicalMemory } else { $null })
  os_caption = $(if ($os) { [string]$os.Caption } else { '' })
  os_version = $(if ($os) { [string]$os.Version } else { '' })
  os_build = $(if ($os) { [string]$os.BuildNumber } else { '' })
  last_boot = $(if ($os) { $os.LastBootUpTime.ToString('o') } else { '' })
  cpu = $(if ($cpu) { [string]$cpu.Name } else { '' })
  usb_controllers = @($ctrl)
  antivirus = @($av)
})
"""


class WindowsPlatform(Platform):
    name = "windows"
    supports_format = True
    supports_reset = True
    supports_inventory = True

    def __init__(self) -> None:
        self._system_disk = win32.system_disk_number()

    # -- volumes ----------------------------------------------------------------------------
    # All file I/O goes through the volume GUID path (\\?\Volume{...}\), which belongs to
    # one partition of one stick. A drive letter can pass to another stick after a
    # disconnect; the GUID path cannot. Letters are kept for display only.
    def _volume(self, guid: str, kind: int | None = None) -> Volume | None:
        kind = win32.drive_type(guid) if kind is None else kind
        if kind not in (win32.DRIVE_REMOVABLE, win32.DRIVE_FIXED):
            return None
        info = win32.volume_information(guid)
        if info is None:  # no media, unreadable, or RAW
            info = {"label": "", "fs": "", "vsn": None, "read_only": False}
        letter = ""
        for path in win32.mount_paths(guid):
            if len(path) == 3 and path[1:] == ":\\":
                letter = path[0].upper()
                break
        if letter and letter == os.environ.get("SystemDrive", "C:")[0].upper():
            return None
        total = free = cluster = 0
        if info["fs"]:
            try:
                total, free, cluster = win32.disk_free(guid)
            except OSError:
                pass
        return Volume(root=guid, letter=letter, guid=guid, label=info["label"], fs=info["fs"],
                      total=total, free=free, cluster=cluster, removable=kind == win32.DRIVE_REMOVABLE,
                      read_only=bool(info.get("read_only")), vsn=info.get("vsn"))

    def list_volumes(self) -> list[Volume]:
        volumes: list[Volume] = []
        for guid in win32.volume_guids():
            vol = self._volume(guid)
            if vol is not None:
                volumes.append(vol)
        return volumes

    def volume_at(self, root: str) -> Volume | None:
        """The volume at one root only (does not touch the other sticks)."""
        guid = self._volume_root(root)
        if not guid.startswith("\\\\?\\Volume"):
            guid = win32.guid_for_root(guid) or guid
        return self._volume(guid)

    def usage(self, root: str) -> tuple[int, int, int]:
        return win32.disk_free(self._volume_root(root))

    def is_read_only(self, root: str) -> bool:
        info = win32.volume_information(self._volume_root(root))
        if info is None:
            raise OSError(0, "volume information unavailable", root, 21)
        return bool(info["read_only"])

    def present(self, root: str) -> bool:
        return win32.volume_information(self._volume_root(root)) is not None

    def storage_info(self, root: str) -> StorageInfo | None:
        device = win32.device_path_for_root(self._volume_root(root))
        try:
            desc = win32.storage_descriptor(device)
        except OSError:
            return None
        number = None
        try:
            number = win32.device_number(device)[1]
        except OSError:
            pass
        return StorageInfo(bus=desc["bus"], removable_media=desc["removable_media"],
                           vendor=desc["vendor"], product=desc["product"],
                           revision=desc["revision"], serial=desc["serial"], disk_number=number,
                           system_disk=number is not None and number == self._system_disk)

    def raw_volume(self, vol: Volume) -> RawVolume:
        device = win32.device_path_for_root(vol.guid or self._volume_root(vol.root))
        raw = win32.RawWin(device)

        class _Raw(RawVolume):
            def read(self, offset: int, length: int) -> bytes:
                return raw.read(offset, length)

            def close(self) -> None:
                raw.close()

        return _Raw()

    @staticmethod
    def _volume_root(root: str) -> str:
        """The root of the volume holding ``root`` (``E:\\FLASHREL-DATA`` -> ``E:\\``)."""
        if len(root) >= 2 and root[1] == ":":
            return root[:2] + "\\"
        match = re.match(r"^(\\\\\?\\Volume\{[0-9a-fA-F-]+\}\\)", root)
        return match.group(1) if match else root

    # -- data path --------------------------------------------------------------------------
    def read_chunks(self, path: str, chunk_size: int, *, uncached: bool = True) -> Iterator[bytes]:
        if uncached:
            return win32.read_unbuffered(path, chunk_size)
        return super().read_chunks(path, chunk_size, uncached=False)

    def cache_bypass(self) -> bool:
        return True

    # -- devices ----------------------------------------------------------------------------
    def inventory(self) -> list[dict[str, Any]]:
        disks = run_powershell(INVENTORY_PS, timeout=120)
        if isinstance(disks, dict):
            disks = [disks]
        for disk in disks:
            disk["serial"] = (disk.get("serial") or "").strip()
            key, chain = location_key(disk.get("location_paths"))
            disk["hub_key"], disk["port_chain"] = key, chain
            for vol in disk.get("volumes") or []:
                vol["letter"] = (vol.get("letter") or "").replace("\x00", "").strip()
            match = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", disk.get("usb_instance") or "",
                              re.I)
            disk["vid_pid"] = f"{match.group(1)}:{match.group(2)}".upper() if match else ""
        return disks

    def format_volume(self, vol: Volume, label: str) -> dict[str, Any]:
        if not self.is_admin():
            raise Unsupported("formatting needs administrator rights")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,11}", label):
            raise ValueError(f"bad volume label {label!r}")
        target = vol.letter or vol.guid
        script = FORMAT_PS.replace("__TARGET__", target.replace("'", "''")).replace("__LABEL__", label)
        result = run_powershell(script, timeout=600)
        if not result.get("ok"):
            raise OSError(0, f"Format-Volume failed: {result.get('error')}", target)
        return result

    def reset_device(self, instance_id: str) -> dict[str, Any]:
        """Restart a USB device (as if it were re-plugged). Never reboots the PC.

        ``pnputil /restart-device`` (Windows 10 2004 and later) is used; its exit code 3010
        means Windows could not restart the device without a reboot (vetoed, e.g. a file
        still open), which is recorded and left alone. The Disable/Enable fallback, for
        older builds only, always re-enables the device, so a stick is never left disabled.
        """
        if not self.is_admin():
            raise Unsupported("device restart needs administrator rights")
        pnputil = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "pnputil.exe")
        text = ""
        try:
            proc = subprocess.run([pnputil, "/restart-device", instance_id], capture_output=True,
                                  timeout=180, creationflags=CREATE_NO_WINDOW)
            text = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
            if proc.returncode in (0, 3010):
                vetoed = proc.returncode == 3010 or "reboot" in text.lower()
                return {"method": "pnputil", "ok": not vetoed, "exit": proc.returncode,
                        "vetoed": vetoed, "output": text[-400:]}
        except subprocess.TimeoutExpired:
            return {"method": "pnputil", "ok": False, "output": "pnputil did not finish within 180 s"}
        except OSError as exc:
            text = str(exc)
        dev = _ps_quote(instance_id)
        script = ("$r = @{ok=$false}; "
                  f"try {{ Disable-PnpDevice -InstanceId {dev} -Confirm:$false; Start-Sleep -Seconds 3; $r.ok = $true }} "
                  "catch { $r.error = $_.Exception.Message } "
                  f"finally {{ try {{ Enable-PnpDevice -InstanceId {dev} -Confirm:$false }} "
                  "catch { $r.ok = $false; $r.enable_error = $_.Exception.Message } }; "
                  "ConvertTo-Json -Compress -InputObject $r")
        try:
            result = run_powershell(script, timeout=180)
        except RuntimeError as exc:
            result = {"ok": False, "error": str(exc)}
            try:  # make sure the stick is not left disabled
                run_powershell(f"Enable-PnpDevice -InstanceId {dev} -Confirm:$false; "
                               "ConvertTo-Json -Compress -InputObject @{ok=$true}", timeout=120)
            except RuntimeError as exc2:
                result["enable_error"] = str(exc2)
        return {"method": "disable-enable", "ok": bool(result.get("ok")),
                "output": str(result.get("error") or result.get("enable_error") or "")[:400],
                "pnputil": text[-200:]}

    # -- host -------------------------------------------------------------------------------
    def keep_awake(self, on: bool) -> None:
        win32.keep_awake(on)

    def is_admin(self) -> bool:
        return win32.is_admin()

    def host_info(self) -> dict[str, Any]:
        info = super().host_info()
        w = sys.getwindowsversion()  # a struct sequence: no _asdict()
        info["windows"] = {k: getattr(w, k, None) for k in ("major", "minor", "build", "platform",
                                                             "service_pack", "product_type")}
        info["admin"] = self.is_admin()
        info["power"] = win32.power_status()
        try:
            info.update(run_powershell(HOSTINFO_PS, timeout=90))
        except Exception as exc:  # informational only
            info["hostinfo_error"] = str(exc)[:300]
        return info

    def python_executable(self) -> str:
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):
            candidate = exe[: -len("pythonw.exe")] + "python.exe"
            if os.path.exists(candidate):
                return candidate
        return exe

    def spawn(self, argv: list[str], log_path: str, cwd: str | None = None,
              env: dict[str, str] | None = None) -> subprocess.Popen:
        log = open(log_path, "ab", buffering=0)
        try:
            return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=cwd, env=env, close_fds=True,
                                    creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        finally:
            log.close()

    def open_url(self, url: str) -> None:
        # explorer.exe hands the URL to the user's (non-elevated) default browser.
        try:
            subprocess.Popen(["explorer.exe", url], creationflags=CREATE_NO_WINDOW)
        except OSError:
            super().open_url(url)
