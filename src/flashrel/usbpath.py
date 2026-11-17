"""USB location paths (Windows device properties), parsed without any Windows API."""

from __future__ import annotations

import re
from collections.abc import Iterable


def location_key(paths: Iterable[str] | None) -> tuple[str, str]:
    """(hub key, port chain) from the USB location paths of a device.

    ``PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3)#USB(2)#USB(4)`` -> hub key
    ``PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3)`` (the PC port the hub is plugged into) and
    port chain ``2.4`` (the path through the hub's internal hub chips). Drives on the same
    hub share a hub key; the two hubs must have different keys.
    """
    paths = [p for p in (paths or []) if p]
    for marker in ("USBROOT(", "ACPI(RHUB)"):
        for path in paths:
            parts = path.split("#")
            for i, part in enumerate(parts):
                if part.startswith(marker) and i + 1 < len(parts):
                    key = "#".join(parts[: i + 2])
                    chain = ".".join(re.sub(r"\D", "", p) or "?" for p in parts[i + 2:])
                    return key, chain
    return "", ""
