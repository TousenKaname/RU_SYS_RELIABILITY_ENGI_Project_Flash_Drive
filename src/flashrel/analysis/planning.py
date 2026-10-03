"""Test-planning arithmetic: cycle times, achievable cycles, expected failures.

All throughput figures are sustained rates in decimal MB/s. The functions are
deliberately simple so that every number in the test plan can be checked by
hand.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
from scipy import stats

#: Fraction of the marketed capacity that an exFAT volume offers (GB vs GiB, file system).
USABLE_FRACTION = 0.93


def cycle_hours(capacity_gb: float, write_mbps: float, read_mbps: float, *,
                fill_fraction: float = 0.90, overhead_s: float = 60.0,
                usable_fraction: float = USABLE_FRACTION) -> float:
    """Hours for one write-verify-delete cycle that fills ``fill_fraction`` of the drive."""
    payload = capacity_gb * 1e9 * usable_fraction * fill_fraction
    seconds = payload / (write_mbps * 1e6) + payload / (read_mbps * 1e6) + overhead_s
    return seconds / 3600.0


def rotation_hours(capacity_gb: float, speeds: Mapping[str, tuple[float, float]],
                   **kwargs: float) -> float:
    """Mean hours per cycle over a rotation; ``speeds`` maps workload -> (write, read)."""
    return float(np.mean([cycle_hours(capacity_gb, w, r, **kwargs) for w, r in speeds.values()]))


def cycles_in(days: float, hours_per_cycle: float) -> float:
    return days * 24.0 / hours_per_cycle


def weibull_cdf(t, shape: float, scale: float):
    return 1.0 - np.exp(-(np.asarray(t, float) / scale) ** shape)


def expected_failures(n: int, t: float, shape: float, scale: float) -> float:
    return float(n * weibull_cdf(t, shape, scale))


def prob_at_least(r: int, n: int, p: float) -> float:
    """P(at least ``r`` of ``n`` units fail) when each fails with probability ``p``."""
    return float(stats.binom.sf(r - 1, n, p))


def units_for_failures(r: int, p: float, assurance: float = 0.90, n_max: int = 10_000) -> int:
    """Smallest sample size that yields ``r`` failures with probability ``assurance``."""
    if not 0 < p <= 1:
        raise ValueError("p must be in (0, 1]")
    for n in range(r, n_max + 1):
        if prob_at_least(r, n, p) >= assurance:
            return n
    raise ValueError("no sample size up to n_max reaches the assurance")
