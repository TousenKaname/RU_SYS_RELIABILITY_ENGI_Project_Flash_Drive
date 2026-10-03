"""Keep the host awake for the length of a run.

A sleeping laptop silently pauses the test and, on wake, can re-enumerate the
USB bus, which would look like a disconnect of every drive at once.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def keep_awake() -> Iterator[None]:
    """Block system sleep while the context is active (display may still sleep)."""
    if sys.platform == "win32":
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
        try:
            yield
        finally:
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)
    elif sys.platform == "darwin":
        proc = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
        try:
            yield
        finally:
            proc.terminate()
    else:
        yield
