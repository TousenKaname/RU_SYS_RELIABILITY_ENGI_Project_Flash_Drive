"""Live life-data statistics for the dashboard (standard library only).

The final analysis is done afterwards with the full toolset; these estimates let the
operator see the survival curves and the Weibull fit grow while the test runs.

Notation as in the course text (Elsayed): R(t) = exp[-(t/theta)^gamma]; here the shape is
``beta`` and the characteristic life ``eta``.
"""

from __future__ import annotations

import math
from typing import Any


def kaplan_meier(times: list[float], events: list[int]) -> list[dict[str, float]]:
    """Kaplan-Meier steps with Greenwood 90 % bounds (failures before censorings at ties)."""
    data = sorted(zip(times, events), key=lambda x: (x[0], -x[1]))
    n = len(data)
    s, var_sum = 1.0, 0.0
    steps = [{"t": 0.0, "s": 1.0, "lo": 1.0, "hi": 1.0, "at_risk": n, "d": 0}]
    i = 0
    while i < n:
        t = data[i][0]
        at_risk = n - i
        d = c = 0
        while i < n and data[i][0] == t:
            d += data[i][1]
            c += 1 - data[i][1]
            i += 1
        if d:
            s *= 1 - d / at_risk
            if at_risk > d:
                var_sum += d / (at_risk * (at_risk - d))
            half = 1.645 * s * math.sqrt(var_sum)
            steps.append({"t": float(t), "s": s, "lo": max(0.0, s - half), "hi": min(1.0, s + half),
                          "at_risk": at_risk, "d": d})
    return steps


def weibull_mle(times: list[float], events: list[int]) -> dict[str, Any] | None:
    """Two-parameter Weibull MLE with right censoring (profile likelihood in beta).

    Solves  sum t^b ln t / sum t^b - 1/b - mean(ln t_failed) = 0  by bisection; the
    left side increases with b. Returns None with fewer than two failures.
    """
    pairs = [(float(t), int(e)) for t, e in zip(times, events) if t and t > 0]
    r = sum(e for _, e in pairs)
    if r < 2:
        return None
    tmax = max(t for t, _ in pairs)
    x = [(t / tmax, e) for t, e in pairs]
    mean_log_fail = sum(math.log(t) for t, e in x if e) / r

    def g(b: float) -> float:
        w = [t ** b for t, _ in x]
        sw = sum(w)
        return sum(wi * math.log(t) for wi, (t, _) in zip(w, x)) / sw - 1.0 / b - mean_log_fail

    lo, hi = 0.02, 60.0
    if g(hi) < 0:
        beta, degenerate = hi, True
    else:
        degenerate = False
        for _ in range(200):
            mid = math.sqrt(lo * hi)
            if g(mid) > 0:
                hi = mid
            else:
                lo = mid
            if hi / lo < 1 + 1e-10:
                break
        beta = math.sqrt(lo * hi)
    eta = tmax * (sum(t ** beta for t, _ in x) / r) ** (1.0 / beta)
    loglik = sum((math.log(beta / eta) + (beta - 1) * math.log(t / eta)) if e else 0.0 for t, e in pairs) \
        - sum((t / eta) ** beta for t, _ in pairs)
    return {"beta": beta, "eta": eta, "mttf": eta * math.gamma(1 + 1 / beta),
            "b10": eta * (-math.log(0.9)) ** (1 / beta), "n": len(pairs), "r": r,
            "loglik": loglik, "degenerate": degenerate}


def _gammainc_lower(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x) (Numerical Recipes, series / fraction)."""
    if x <= 0:
        return 0.0
    gln = math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(500):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return total * math.exp(-x + a * math.log(x) - gln)
    b = x + 1 - a
    c, d = 1e300, 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        h *= d * c
        if abs(d * c - 1) < 1e-14:
            break
    return 1 - math.exp(-x + a * math.log(x) - gln) * h


def chi2_quantile(p: float, dof: int) -> float:
    lo, hi = 0.0, max(10.0, dof * 10.0)
    for _ in range(200):
        mid = (lo + hi) / 2
        if _gammainc_lower(dof / 2, mid / 2) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def weibayes_lower(times: list[float], events: list[int], beta: float, conf: float = 0.90) -> float | None:
    """Weibayes lower bound on eta at confidence ``conf`` for an assumed shape ``beta``."""
    ts = [float(t) for t in times if t and t > 0]
    if not ts:
        return None
    r = sum(events)
    return (2 * sum(t ** beta for t in ts) / chi2_quantile(conf, 2 * r + 2)) ** (1 / beta)


def group_summary(rows: list[dict[str, Any]], unit: str = "cycles") -> dict[str, Any]:
    """rows: [{"group", "t", "event"}] -> per group: n, failures, KM steps, Weibull fit."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    out: dict[str, Any] = {"unit": unit, "groups": {}}
    for name, items in sorted(groups.items()):
        t = [float(i["t"]) for i in items]
        e = [int(i["event"]) for i in items]
        fit = weibull_mle(t, e)
        out["groups"][name] = {
            "n": len(items), "failures": sum(e), "km": kaplan_meier(t, e),
            "censored": sorted(ti for ti, ei in zip(t, e) if not ei),
            "weibull": fit, "weibayes_b2": None if fit else weibayes_lower(t, e, 2.0),
        }
    return out
