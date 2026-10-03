"""Temperature sources.

USB flash drives report no temperature, so the rig measures it from outside:
DS18B20 probes taped to the drive housings (and one in free air) are read by
an Arduino, which prints one ``<probe-rom>,<celsius>`` line per probe (see
``hardware/arduino``). The supervisor samples the source every few minutes and
logs the readings next to the cycle records; the analysis joins them by time.

Any object with ``read()`` and ``close()`` methods works as a source, which keeps
the harness independent of the sensor hardware.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Protocol


class TemperatureSource(Protocol):
    def read(self) -> dict[str, float]:
        """Latest reading of every probe, keyed by logical probe name."""

    def close(self) -> None: ...


class NullTemperatureSource:
    """Used when no sensor is attached; logs nothing."""

    def read(self) -> dict[str, float]:
        return {}

    def close(self) -> None:
        pass


class StaticTemperatureSource:
    """Fixed readings, for tests and dry runs."""

    def __init__(self, readings: Mapping[str, float]) -> None:
        self._readings = dict(readings)

    def read(self) -> dict[str, float]:
        return dict(self._readings)

    def close(self) -> None:
        pass


def parse_line(line: str, names: Mapping[str, str]) -> tuple[str, float] | None:
    """Parse ``"28FF4A...,31.25"``; map the probe ROM code to a logical name."""
    parts = line.strip().split(",")
    if len(parts) != 2:
        return None
    rom, value = parts[0].strip().upper(), parts[1].strip()
    try:
        celsius = float(value)
    except ValueError:
        return None
    if not -55.0 <= celsius <= 125.0:  # DS18B20 range; -127 and 85 are error codes
        return None
    if celsius == 85.0:  # power-on reset value, never a real reading here
        return None
    return names.get(rom, rom), celsius


class SerialTemperatureSource:
    """Reads an Arduino over a serial port in a background thread.

    ``names`` maps probe ROM codes to logical names such as ``"A8-03"`` or
    ``"ambient-W"``. Requires ``pyserial`` (``pip install flashrel[sensors]``).
    """

    def __init__(self, port: str, names: Mapping[str, str] | None = None,
                 baudrate: int = 9600) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("SerialTemperatureSource needs pyserial: "
                               "pip install 'flashrel[sensors]'") from exc
        self._names = {k.upper(): v for k, v in (names or {}).items()}
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=2)
        self._latest: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="temperature", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._serial.readline()
            except Exception:  # unplugged Arduino: keep the test running
                self._stop.wait(5)
                continue
            parsed = parse_line(raw.decode("ascii", errors="ignore"), self._names)
            if parsed:
                with self._lock:
                    self._latest[parsed[0]] = parsed[1]

    def read(self) -> dict[str, float]:
        with self._lock:
            return dict(self._latest)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._serial.close()
