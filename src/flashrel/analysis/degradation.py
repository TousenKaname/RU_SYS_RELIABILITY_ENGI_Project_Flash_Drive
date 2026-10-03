"""Degradation analysis of write throughput.

When few drives fail before the test ends, their *slowing down* still carries
information about life (Lu and Meeker, 1993). For each drive and workload the
write throughput is normalised by the drive's own baseline, a log-linear path
ln y = a + b * cycle is fitted, and the cycle at which the path crosses a
threshold (50 % of baseline by default) is a pseudo-failure time. Drives whose
path does not fall, or would cross far beyond the observed range, are censored
at their last cycle.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def normalised_paths(cycles: pd.DataFrame, metric: str = "write_mbps",
                     baseline_cycles: int = 2) -> pd.DataFrame:
    """Add ``baseline`` and ``relative`` columns per (drive, workload) path."""
    ok = cycles[cycles["outcome"] == "pass"].sort_values("cycle").copy()
    parts = []
    for (_, _), path in ok.groupby(["drive_id", "workload"], sort=False):
        base = path[metric].iloc[:baseline_cycles].median()
        path = path.assign(baseline=base, relative=path[metric] / base)
        parts.append(path)
    return pd.concat(parts, ignore_index=True) if parts else ok.assign(baseline=[], relative=[])


def fit_paths(paths: pd.DataFrame, threshold: float = 0.5,
              max_extrapolation: float = 3.0) -> pd.DataFrame:
    """Per-path log-linear fit and pseudo-failure cycle at ``threshold``."""
    rows = []
    for (drive, workload), path in paths.groupby(["drive_id", "workload"], sort=True):
        c = path["cycle"].to_numpy(float)
        y = np.log(np.clip(path["relative"].to_numpy(float), 1e-6, None))
        last = float(c.max())
        if len(c) < 3:
            rows.append((drive, workload, np.nan, np.nan, np.nan, last, last, False))
            continue
        slope, intercept = np.polyfit(c, y, 1)
        resid = y - (intercept + slope * c)
        crossing = (np.log(threshold) - intercept) / slope if slope < 0 else np.inf
        failed = bool(np.isfinite(crossing) and crossing <= max_extrapolation * last)
        time = max(crossing, 1.0) if failed else last
        rows.append((drive, workload, intercept, slope, float(np.std(resid, ddof=2)
                     if len(c) > 2 else np.nan), last, time, failed))
    return pd.DataFrame(rows, columns=["drive_id", "workload", "intercept", "slope", "resid_sd",
                                       "last_cycle", "pseudo_time", "pseudo_failure"])
