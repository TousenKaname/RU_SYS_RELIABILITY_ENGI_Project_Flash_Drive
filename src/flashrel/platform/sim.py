"""Simulated drives: real files in folders, speeds and failures made up.

Used by the automated tests and by ``flashrel app --sim`` (a demo of the whole rig,
dashboard included, on any computer). Nothing here is a measurement.

    <root>/devices/<serial>.json        device: model, size, hub/port, speeds, fault model
    <root>/devices/<serial>.wear.json   wear counters and fault state (written by the worker)
    <root>/volumes/<serial>/            the mounted volume (renamed away while "unplugged")

Fault modes (drawn per device, triggered after a random number of file operations):
``metadata`` (identity/sentinel files rot on the "flash"), ``missing`` (part of a
directory is lost), ``corrupt`` (read-back returns flipped bits), ``readonly``,
``disconnect`` (the drive drops off the bus, the first time only briefly), ``io`` and
``raw`` (the file system no longer mounts: the stick stays on the bus, but every file
operation fails, as on Windows for a RAW volume).
"""

from __future__ import annotations

import errno
import json
import math
import os
import random
import shutil
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

from flashrel.platform.base import Platform, StorageInfo, Unsupported, Volume
from flashrel.units import KiB, MiB

CLUSTER = 32 * KiB
IDENTITY_NAMES = ("FLASHREL-ID.json", "FLASHREL-SENTINEL.bin")

# Group behaviour of the demo (loosely shaped after run 1; not data).
PROFILES = {
    "A8": {"capacity": 48 * MiB, "write": 11.5, "read": 16.5, "create_ms": 90, "delete_ms": 300,
           "eta_ops": 2600, "beta": 2.2,
           "modes": {"metadata": 0.45, "raw": 0.1, "missing": 0.15, "corrupt": 0.15, "disconnect": 0.15}},
    "A16": {"capacity": 96 * MiB, "write": 7.0, "read": 12.5, "create_ms": 35, "delete_ms": 40,
            "eta_ops": 7000, "beta": 2.0,
            "modes": {"missing": 0.5, "corrupt": 0.25, "readonly": 0.1, "disconnect": 0.15}},
    "S8": {"capacity": 48 * MiB, "write": 3.9, "read": 24.8, "create_ms": 50, "delete_ms": 8,
           "eta_ops": 60000, "beta": 1.5, "modes": {"readonly": 0.5, "io": 0.5}},
}
TRANSIENT_RATE = 1 / 4000  # per file read: an error that clears on retry (D3)


def _atomic_json(path: str, data: dict) -> None:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # threads of one process too
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    for _ in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02)
    os.replace(tmp, path)


def _load(path: str, default: dict | None = None) -> dict:
    for _ in range(20):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return dict(default or {})
        except (ValueError, PermissionError):
            time.sleep(0.02)
    return dict(default or {})


class SimPlatform(Platform):
    name = "sim"
    supports_format = True
    supports_reset = True
    supports_inventory = True

    def __init__(self, root: str, speed: float = 1.0) -> None:
        self.root = os.path.abspath(root)
        self.speed = max(1e-6, float(speed))
        self.dev_dir = os.path.join(self.root, "devices")
        self.vol_dir = os.path.join(self.root, "volumes")
        os.makedirs(self.dev_dir, exist_ok=True)
        os.makedirs(self.vol_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._rng = random.Random()

    # -- rig construction -------------------------------------------------------------------
    def create_rig(self, campaign, seed: int = 1) -> None:
        """One simulated device per unit of the campaign (test drives on their hub ports,
        spares on a bench hub "S"). Idempotent."""
        rng = random.Random(seed)
        bench = 0
        for unit in campaign.all_units():
            group = campaign.group_of(unit).id
            prof = PROFILES.get(group, PROFILES["A8"])
            slot = campaign.slot_of(unit)
            if slot is None:
                bench += 1
                hub, port = "S", bench
            else:
                hub, port = slot.hub, slot.port
            serial = f"SIM{rng.randrange(10**12):012d}"
            if self._find_by_hint(unit):
                continue
            mode_names, weights = zip(*prof["modes"].items())
            dev = {
                "serial": serial, "hint": unit, "group": group,
                "vendor": "SanDisk" if group == "S8" else "VendorCo",
                "product": "Cruzer Blade" if group == "S8" else "ProductCode",
                "capacity": int(campaign.nominal_bytes(unit)), "hub": hub, "port": port,
                "write_mbps": prof["write"], "read_mbps": prof["read"],
                "create_ms": prof["create_ms"], "delete_ms": prof["delete_ms"],
                "fail_after_ops": max(400, int(prof["eta_ops"] * (-math.log(1 - rng.random())) ** (1 / prof["beta"]))),
                "mode": rng.choices(mode_names, weights)[0],
                "fs": "FAT32", "label": "NO NAME", "absent_until": 0.0, "gone": False,
                "read_only": False,
            }
            _atomic_json(self._dev_path(serial), dev)
            vol = os.path.join(self.vol_dir, serial)
            os.makedirs(vol, exist_ok=True)
            with open(os.path.join(vol, "README.txt"), "w") as fh:
                fh.write("factory content\n")

    def _find_by_hint(self, unit: str) -> dict | None:
        for dev in self.devices():
            if dev.get("hint") == unit:
                return dev
        return None

    # -- registry ---------------------------------------------------------------------------
    def _dev_path(self, serial: str) -> str:
        return os.path.join(self.dev_dir, f"{serial}.json")

    def _wear_path(self, serial: str) -> str:
        return os.path.join(self.dev_dir, f"{serial}.wear.json")

    def devices(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.dev_dir)):
            if name.endswith(".json") and not name.endswith(".wear.json") and ".tmp" not in name:
                dev = _load(os.path.join(self.dev_dir, name))
                if dev:
                    out.append(dev)
        return out

    def device(self, serial: str) -> dict:
        return _load(self._dev_path(serial))

    def update_device(self, serial: str, **changes: Any) -> None:
        with self._lock:
            dev = self.device(serial)
            dev.update(changes)
            _atomic_json(self._dev_path(serial), dev)

    def _serial_of(self, path: str) -> str | None:
        rel = os.path.relpath(os.path.abspath(path), self.vol_dir)
        head = rel.split(os.sep, 1)[0]
        return None if head.startswith("..") else head

    def _volume_path(self, serial: str) -> str:
        return os.path.join(self.vol_dir, serial)

    def _gone_path(self, serial: str) -> str:
        return os.path.join(self.vol_dir, f".gone-{serial}")

    def _settle(self, dev: dict) -> dict:
        """Bring back a drive whose temporary disconnect has expired."""
        serial = dev["serial"]
        if not dev.get("gone") and dev.get("absent_until", 0) and time.time() >= dev["absent_until"]:
            if os.path.isdir(self._gone_path(serial)) and not os.path.isdir(self._volume_path(serial)):
                try:
                    os.rename(self._gone_path(serial), self._volume_path(serial))
                except OSError:
                    pass
            self.update_device(serial, absent_until=0.0)
            dev = self.device(serial)
        return dev

    def _present(self, dev: dict) -> bool:
        """On the bus (the volume may still be RAW)."""
        dev = self._settle(dev)
        return os.path.isdir(self._volume_path(dev["serial"]))

    def _unusable(self, serial: str | None, path: str) -> None:
        if serial and not os.path.isdir(self._volume_path(serial)):
            raise OSError(errno.ENXIO, "device not configured", path)
        if serial and self.device(serial).get("raw"):
            raise OSError(errno.EIO, "the volume does not contain a recognized file system", path)

    def unplug(self, serial: str, seconds: float | None) -> None:
        """Take a drive off the bus for ``seconds`` (None = until re-plugged by hand)."""
        if os.path.isdir(self._volume_path(serial)):
            try:
                os.rename(self._volume_path(serial), self._gone_path(serial))
            except OSError:
                return
        if seconds is None:
            self.update_device(serial, gone=True, absent_until=0.0)
        else:
            self.update_device(serial, gone=False, absent_until=time.time() + seconds)

    def replug(self, serial: str) -> None:
        if os.path.isdir(self._gone_path(serial)) and not os.path.isdir(self._volume_path(serial)):
            os.rename(self._gone_path(serial), self._volume_path(serial))
        self.update_device(serial, gone=False, absent_until=0.0)

    def move(self, serial: str, hub: str, port: int) -> None:
        """Re-plug a simulated stick into another hub port (post-test protocol)."""
        self.update_device(serial, hub=hub, port=int(port))

    def hub_glitch(self, hub: str, seconds: float) -> list[str]:
        hit = [d["serial"] for d in self.devices() if d["hub"] == hub]
        for serial in hit:
            self.unplug(serial, seconds)
        return hit

    # -- wear and faults --------------------------------------------------------------------
    def _wear(self, serial: str) -> dict:
        return _load(self._wear_path(serial), {"bytes_written": 0, "bytes_read": 0, "ops": 0,
                                               "triggered": False, "disconnects": 0})

    def _save_wear(self, serial: str, wear: dict) -> None:
        _atomic_json(self._wear_path(serial), wear)

    def _count(self, path: str, *, ops: int = 0, written: int = 0, read: int = 0) -> None:
        serial = self._serial_of(path)
        if not serial:
            return
        wear = self._wear(serial)
        wear["ops"] += ops
        wear["bytes_written"] += written
        wear["bytes_read"] += read
        dev = self.device(serial)
        threshold = dev.get("fail_after_ops")
        if threshold is not None and not wear["triggered"] and wear["ops"] >= threshold:
            wear["triggered"] = True
            wear["triggered_at_ops"] = wear["ops"]
            self._save_wear(serial, wear)
            self._trigger(dev, path)
            return
        if (dev.get("mode") == "disconnect" and wear["triggered"] and wear.get("disconnects") == 1
                and wear["ops"] >= wear.get("triggered_at_ops", 0) * 1.3):
            wear["disconnects"] = 2
            self._save_wear(serial, wear)
            self.unplug(serial, None)  # the second drop is for good
            return
        self._save_wear(serial, wear)

    def _trigger(self, dev: dict, path: str) -> None:
        serial, mode = dev["serial"], dev.get("mode")
        vol = self._volume_path(serial)
        if mode == "metadata":
            for name in IDENTITY_NAMES:
                _flip_file(os.path.join(vol, name), fraction=0.1)
        elif mode == "missing":
            directory = os.path.dirname(path)
            names = sorted(n for n in os.listdir(directory) if n.endswith(".bin")) if os.path.isdir(directory) else []
            for name in names[len(names) // 3: len(names) // 3 + max(1, len(names) // 12)]:
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass
        elif mode == "readonly":
            self.update_device(serial, read_only=True)
        elif mode == "disconnect":
            wear = self._wear(serial)
            wear["disconnects"] = 1
            self._save_wear(serial, wear)
            self.unplug(serial, 6.0 / max(1.0, self.speed ** 0.5))
        elif mode == "raw":
            self.update_device(serial, raw=True)

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds / self.speed)

    # -- Platform interface -----------------------------------------------------------------
    def list_volumes(self) -> list[Volume]:
        vols = []
        for i, dev in enumerate(self.devices()):
            if not self._present(dev):
                continue
            root = self._volume_path(dev["serial"]) + os.sep
            raw = bool(dev.get("raw"))
            total, free, cluster = (0, 0, 0) if raw else self.usage(root)
            vols.append(Volume(root=root, letter=chr(ord("D") + i) if i < 23 else "",
                               guid=f"\\\\?\\Volume{{sim-{dev['serial']}}}\\", label="" if raw else dev.get("label", ""),
                               fs="" if raw else dev.get("fs", ""), total=total, free=free, cluster=cluster,
                               removable=True, read_only=bool(dev.get("read_only")),
                               vsn=int(dev["serial"][3:9])))
        return vols

    def usage(self, root: str) -> tuple[int, int, int]:
        serial = self._serial_of(root)
        dev = self.device(serial) if serial else {}
        vol = self._volume_path(serial) if serial else root
        if not dev or not os.path.isdir(vol):
            raise OSError(errno.ENXIO, "device not configured", root)
        self._unusable(serial, root)
        used = 0
        for base, _dirs, files in os.walk(vol):
            for name in files:
                try:
                    size = os.path.getsize(os.path.join(base, name))
                except OSError:
                    continue
                used += -(-size // CLUSTER) * CLUSTER
            used += CLUSTER * max(0, len(_dirs))
        return dev["capacity"], max(0, dev["capacity"] - used), CLUSTER

    def is_read_only(self, root: str) -> bool:
        serial = self._serial_of(root)
        if not serial:
            raise OSError(errno.ENXIO, "device not configured", root)
        self._unusable(serial, root)
        return bool(self.device(serial).get("read_only"))

    def present(self, root: str) -> bool:
        serial = self._serial_of(root)
        return bool(serial) and self._present(self.device(serial)) and not self.device(serial).get("raw")

    def attached(self, root: str) -> bool:
        serial = self._serial_of(root)
        return bool(serial) and self._present(self.device(serial))

    def storage_info(self, root: str) -> StorageInfo | None:
        serial = self._serial_of(root)
        if not serial:
            return None
        dev = self.device(serial)
        return StorageInfo(bus="USB", removable_media=True, vendor=dev.get("vendor", ""),
                           product=dev.get("product", ""), revision="1.00", serial=serial,
                           disk_number=None, system_disk=False)

    def read_chunks(self, path: str, chunk_size: int, *, uncached: bool = True) -> Iterator[bytes]:
        serial = self._serial_of(path)
        dev = self.device(serial) if serial else {}
        wear = self._wear(serial) if serial else {}
        name = os.path.basename(path)
        payload = name.endswith(".bin") and name not in IDENTITY_NAMES
        self._unusable(serial, path)
        if payload and wear.get("triggered") and dev.get("mode") == "io":
            raise OSError(errno.EIO, "Input/output error", path)
        if payload and self._rng.random() < TRANSIENT_RATE:
            raise OSError(errno.EIO, "Input/output error (transient)", path)
        corrupt = payload and wear.get("triggered") and dev.get("mode") == "corrupt"
        total = 0
        for chunk in super().read_chunks(path, chunk_size, uncached=False):
            total += len(chunk)
            if dev:
                self._sleep(len(chunk) / (dev["read_mbps"] * 1e6))
            if corrupt and len(chunk) > 64:
                b = bytearray(chunk)
                for _ in range(4):
                    i = self._rng.randrange(len(b))
                    b[i] ^= 1 << self._rng.randrange(8)
                chunk = bytes(b)
            yield chunk
        self._count(path, read=total)

    def write_file(self, path: str, chunks: Iterable[bytes], *, sync: bool = True) -> int:
        serial = self._serial_of(path)
        dev = self.device(serial) if serial else {}
        self._unusable(serial, path)
        if dev.get("read_only"):
            raise OSError(errno.EROFS, "Read-only file system", path)
        total = 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o644)
        try:
            for chunk in chunks:
                os.write(fd, chunk)
                total += len(chunk)
                if dev:
                    self._sleep(len(chunk) / (dev["write_mbps"] * 1e6))
            if dev:
                self._sleep(dev["create_ms"] / 1000.0)
        finally:
            os.close(fd)
        self._count(path, ops=1, written=total)
        return total

    def remove_file(self, path: str) -> None:
        serial = self._serial_of(path)
        if serial:
            dev = self.device(serial)
            self._unusable(serial, path)
            if dev.get("read_only"):
                raise OSError(errno.EROFS, "Read-only file system", path)
            self._sleep(dev.get("delete_ms", 0) / 1000.0)
        os.remove(path)
        self._count(path, ops=1)

    def inventory(self) -> list[dict[str, Any]]:
        out = []
        for number, dev in enumerate(self.devices(), start=10):
            if not self._present(dev):
                continue
            hub_no = {"A": 1, "B": 2}.get(dev["hub"], 9)
            serial = dev["serial"]
            root = self._volume_path(serial) + os.sep
            try:
                total, free, _ = self.usage(root)
            except OSError:
                total, free = dev["capacity"], 0
            out.append({
                "number": number, "serial": serial, "model": f"{dev['vendor']} {dev['product']}",
                "size": dev["capacity"], "boot": False, "system": False,
                "readonly": bool(dev.get("read_only")), "offline": False, "health": "Healthy",
                "operational": "Online", "disk_instance": f"SIMSTOR\\DISK\\{serial}",
                "usb_instance": f"USB\\VID_5150&PID_{hub_no:04d}\\{serial}",
                "hub_instance": f"SIMHUB\\{dev['hub']}", "hub_name": f"Simulated hub {dev['hub']}",
                "location_info": f"Port_#{dev['port']:04d}.Hub_#{hub_no:04d}",
                "location_paths": [f"PCIROOT(0)#PCI(1400)#USBROOT(0)#USB({hub_no})#USB({dev['port']})"],
                "removal_policy": 3, "hub_key": f"PCIROOT(0)#PCI(1400)#USBROOT(0)#USB({hub_no})",
                "port_chain": str(dev["port"]), "vid_pid": f"5150:{hub_no:04d}",
                "volumes": [{"letter": "", "fs": dev.get("fs", ""), "label": dev.get("label", ""),
                             "size": total, "free": free, "guid": f"\\\\?\\Volume{{sim-{serial}}}\\",
                             "health": "Healthy"}],
            })
        return out

    def format_volume(self, vol: Volume, label: str) -> dict[str, Any]:
        serial = self._serial_of(vol.root)
        if not serial:
            raise Unsupported("not a simulated volume")
        path = self._volume_path(serial)
        for name in os.listdir(path):
            full = os.path.join(path, name)
            shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
        self.update_device(serial, fs="exFAT", label=label, read_only=False, raw=False)
        return {"ok": True, "fs": "exFAT", "label": label}

    def reset_device(self, instance_id: str) -> dict[str, Any]:
        serial = instance_id.rsplit("\\", 1)[-1]
        dev = self.device(serial)
        if not dev:
            return {"method": "sim", "ok": False, "output": "unknown device"}
        if not dev.get("gone") and dev.get("absent_until"):
            self.replug(serial)  # a restart brings a briefly dropped drive back at once
        return {"method": "sim", "ok": True, "output": "restarted"}

    def is_admin(self) -> bool:
        return True

    def cache_bypass(self) -> bool:
        return True


def _flip_file(path: str, fraction: float) -> None:
    try:
        with open(path, "r+b") as fh:
            data = bytearray(fh.read())
            rng = random.Random(len(data))
            for i in range(len(data)):
                if rng.random() < fraction:
                    data[i] ^= 1 << rng.randrange(8)
            fh.seek(0)
            fh.write(data)
    except OSError:
        pass
