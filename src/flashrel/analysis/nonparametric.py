"""Distribution-free estimators for censored life data and recurrent events.

* :func:`kaplan_meier` - product-limit reliability with Greenwood variance;
* :func:`nelson_aalen` - cumulative hazard;
* :func:`median_ranks` - Bernard's median ranks with Johnson's adjustment for
  suspensions (the rank method of the course, extended to censored data);
* :func:`mean_cumulative_function` - Nelson's MCF for repeated soft failures,
  with the Lawless-Nadeau robust variance.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
from scipy import stats


def _sorted(times: Sequence[float], events: Sequence[bool]) -> tuple[np.ndarray, np.ndarray]:
    t = np.asarray(times, dtype=float)
    d = np.asarray(events, dtype=bool)
    if t.shape != d.shape or t.ndim != 1:
        raise ValueError("times and events must be 1-D arrays of equal length")
    if np.any(t < 0):
        raise ValueError("times must be non-negative")
    # Failures sort before suspensions at the same time (the usual convention).
    order = np.lexsort((~d, t))
    return t[order], d[order]


def kaplan_meier(times: Sequence[float], events: Sequence[bool],
                 level: float = 0.90) -> pd.DataFrame:
    """Kaplan-Meier R(t) at each distinct failure time, with log(-log) bounds."""
    t, d = _sorted(times, events)
    z = stats.norm.ppf(0.5 + level / 2)
    rows = []
    surv, green = 1.0, 0.0
    for time in np.unique(t[d]):
        at_risk = int(np.sum(t >= time))
        deaths = int(np.sum((t == time) & d))
        surv *= 1.0 - deaths / at_risk
        if at_risk > deaths:
            green += deaths / (at_risk * (at_risk - deaths))
        if 0.0 < surv < 1.0:
            half = z * np.sqrt(green) / abs(np.log(surv))
            lower = surv ** np.exp(half)
            upper = surv ** np.exp(-half)
        else:
            lower = upper = surv
        rows.append((time, at_risk, deaths, surv, surv * np.sqrt(green), lower, upper))
    return pd.DataFrame(rows, columns=["time", "at_risk", "failures", "reliability", "se",
                                       "lower", "upper"])


def nelson_aalen(times: Sequence[float], events: Sequence[bool]) -> pd.DataFrame:
    t, d = _sorted(times, events)
    rows, cum, var = [], 0.0, 0.0
    for time in np.unique(t[d]):
        at_risk = int(np.sum(t >= time))
        deaths = int(np.sum((t == time) & d))
        cum += deaths / at_risk
        var += deaths / at_risk**2
        rows.append((time, at_risk, deaths, cum, np.sqrt(var)))
    return pd.DataFrame(rows, columns=["time", "at_risk", "failures", "cum_hazard", "se"])


def median_ranks(times: Sequence[float], events: Sequence[bool]) -> pd.DataFrame:
    """Plotting positions F = (O - 0.3)/(N + 0.4) for the failures.

    O is Johnson's adjusted order number, which spreads the probability mass of
    each suspension over the failures that come after it. Without suspensions
    O is simply 1, 2, ..., N.
    """
    t, d = _sorted(times, events)
    n = len(t)
    order, rows = 0.0, []
    for position, (time, failed) in enumerate(zip(t, d), start=1):
        if not failed:
            continue
        reverse_rank = n - position + 1
        order += (n + 1 - order) / (1 + reverse_rank)
        rows.append((time, order, (order - 0.3) / (n + 0.4)))
    return pd.DataFrame(rows, columns=["time", "adjusted_rank", "F"])


def mean_cumulative_function(units: Sequence[tuple[Sequence[float], float]],
                             level: float = 0.90) -> pd.DataFrame:
    """Nelson's MCF for recurrent events.

    ``units`` holds one ``(event_times, end_time)`` pair per drive; a drive is
    under observation from 0 to ``end_time``. The robust variance does not
    assume a Poisson process (Lawless and Nadeau, 1995).
    """
    if not units:
        return pd.DataFrame(columns=["time", "at_risk", "events", "mcf", "se", "lower", "upper"])
    ends = np.array([end for _, end in units], dtype=float)
    per_unit = [np.asarray(ev, dtype=float) for ev, _ in units]
    grid = np.unique(np.concatenate([e for e in per_unit if e.size] or [np.array([])]))
    z = stats.norm.ppf(0.5 + level / 2)
    increments = np.zeros((len(units), len(grid)))
    rows = []
    mcf = 0.0
    for k, time in enumerate(grid):
        observed = ends >= time
        r = int(observed.sum())
        if r == 0:
            break
        # an event counts only while its unit is still under observation
        d_i = np.array([np.sum(e == time) for e in per_unit], dtype=float) * observed
        d = d_i.sum()
        mcf += d / r
        increments[:, k] = np.where(observed, (d_i - d / r) / r, 0.0)
        var = float(np.sum(increments[:, : k + 1].sum(axis=1) ** 2))
        se = np.sqrt(var)
        if mcf > 0:
            factor = np.exp(z * se / mcf)
            lower, upper = mcf / factor, mcf * factor
        else:
            lower = upper = 0.0
        rows.append((time, r, int(d), mcf, se, lower, upper))
    return pd.DataFrame(rows, columns=["time", "at_risk", "events", "mcf", "se", "lower", "upper"])
