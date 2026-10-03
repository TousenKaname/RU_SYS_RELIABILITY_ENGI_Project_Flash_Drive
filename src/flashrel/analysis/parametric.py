"""Parametric life models fitted to right-censored data.

The Weibull model is the main tool: R(t) = exp[-(t/theta)^gamma]. A shape
gamma > 1 means wear-out, which is what finite program/erase endurance
predicts; gamma < 1 means early ("infant") failures, which point to weak units
or a mixture of populations. The lognormal model is fitted as the usual
alternative, and the Weibayes bound covers the case where no drive has failed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import optimize, special, stats

from flashrel.analysis.nonparametric import median_ranks


def _arrays(times: Sequence[float], events: Sequence[bool]) -> tuple[np.ndarray, np.ndarray]:
    t = np.asarray(times, dtype=float)
    d = np.asarray(events, dtype=bool)
    if t.shape != d.shape or t.ndim != 1 or len(t) == 0:
        raise ValueError("times and events must be non-empty 1-D arrays of equal length")
    if np.any(t <= 0):
        raise ValueError("life times must be positive")
    return t, d


def _numeric_hessian(f, x: np.ndarray, step: float = 1e-4) -> np.ndarray:
    k = len(x)
    h = np.zeros((k, k))
    for i in range(k):
        for j in range(i, k):
            ei, ej = np.eye(k)[i] * step, np.eye(k)[j] * step
            h[i, j] = h[j, i] = (f(x + ei + ej) - f(x + ei - ej) - f(x - ei + ej)
                                 + f(x - ei - ej)) / (4 * step * step)
    return h


@dataclass(frozen=True)
class WeibullFit:
    shape: float
    scale: float
    n: int
    failures: int
    loglik: float
    method: str
    cov_log: np.ndarray | None = None  # covariance of (ln theta, ln gamma)

    def reliability(self, t):
        return np.exp(-(np.asarray(t, float) / self.scale) ** self.shape)

    def cdf(self, t):
        return 1.0 - self.reliability(t)

    def hazard(self, t):
        t = np.asarray(t, float)
        return self.shape / self.scale * (t / self.scale) ** (self.shape - 1)

    @property
    def mttf(self) -> float:
        return self.scale * special.gamma(1 + 1 / self.shape)

    def quantile(self, p: float) -> float:
        """Life by which a fraction ``p`` has failed (B10 life = quantile(0.10))."""
        return self.scale * (-np.log1p(-p)) ** (1 / self.shape)

    def mrl(self, t: float) -> float:
        """Mean residual life after surviving ``t`` cycles."""
        a = 1 / self.shape
        x = (t / self.scale) ** self.shape
        tail = self.scale * a * special.gamma(a) * special.gammaincc(a, x)
        return float(tail / np.exp(-x))

    def interval(self, name: str, level: float = 0.90) -> tuple[float, float]:
        """Wald interval on the log scale for 'shape', 'scale' or a quantile 'B10'."""
        if self.cov_log is None:
            return (np.nan, np.nan)
        z = stats.norm.ppf(0.5 + level / 2)
        if name == "scale":
            est, var = self.scale, self.cov_log[0, 0]
        elif name == "shape":
            est, var = self.shape, self.cov_log[1, 1]
        elif name.startswith("B"):
            p = float(name[1:]) / 100
            # ln t_p = ln theta + (1/gamma) ln(-ln(1-p)); gradient w.r.t. (ln theta, ln gamma)
            w = np.log(-np.log1p(-p))
            grad = np.array([1.0, -w / self.shape])
            est, var = self.quantile(p), float(grad @ self.cov_log @ grad)
        else:
            raise ValueError(f"unknown quantity {name!r}")
        half = z * np.sqrt(var)
        return (est * np.exp(-half), est * np.exp(half))


def weibull_loglik(t: np.ndarray, d: np.ndarray, shape: float, scale: float) -> float:
    z = (t / scale) ** shape
    logf = np.log(shape / scale) + (shape - 1) * np.log(t / scale) - z
    return float(np.sum(np.where(d, logf, -z)))


def fit_weibull_mle(times: Sequence[float], events: Sequence[bool]) -> WeibullFit:
    """Maximum-likelihood Weibull fit with right censoring.

    For a fixed shape the likelihood is maximised by
    theta(gamma) = (sum t_j^gamma / r)^(1/gamma), so only the profile equation
    sum t^g ln t / sum t^g - 1/g - mean(ln t_failures) = 0 has to be solved.
    """
    t, d = _arrays(times, events)
    r = int(d.sum())
    if r == 0:
        raise ValueError("no failures: use weibayes_lower_bound() instead")
    ref = np.exp(np.mean(np.log(t)))  # rescale for numerical stability
    u = t / ref
    lu = np.log(u)
    mean_fail = lu[d].mean()

    def profile(g: float) -> float:
        w = np.exp(g * lu - np.max(g * lu))
        return float(np.sum(w * lu) / np.sum(w) - 1 / g - mean_fail)

    lo, hi = 1e-3, 1e3
    if profile(hi) <= 0:
        raise ValueError("the data do not determine a finite shape (all failures at the end)")
    shape = optimize.brentq(profile, lo, hi, xtol=1e-12)
    scale = ref * (np.sum(u**shape) / r) ** (1 / shape)

    def negll(x: np.ndarray) -> float:
        return -weibull_loglik(t, d, np.exp(x[1]), np.exp(x[0]))

    x0 = np.array([np.log(scale), np.log(shape)])
    cov = None
    if r >= 2:
        hess = _numeric_hessian(negll, x0)
        try:
            cov = np.linalg.inv(hess)
        except np.linalg.LinAlgError:
            cov = None
    return WeibullFit(shape, scale, len(t), r, weibull_loglik(t, d, shape, scale), "mle", cov)


def fit_weibull_rank_regression(times: Sequence[float], events: Sequence[bool]) -> WeibullFit:
    """Course method: least squares of ln ln(1/(1-F)) on ln t at median ranks."""
    t, d = _arrays(times, events)
    ranks = median_ranks(t, d)
    if len(ranks) < 2:
        raise ValueError("rank regression needs at least two failures")
    x = np.log(ranks["time"].to_numpy())
    y = np.log(-np.log1p(-ranks["F"].to_numpy()))
    slope, intercept = np.polyfit(x, y, 1)
    shape = slope
    scale = np.exp(-intercept / slope)
    return WeibullFit(shape, scale, len(t), int(d.sum()), weibull_loglik(t, d, shape, scale),
                      "rank_regression")


def weibayes_lower_bound(times: Sequence[float], events: Sequence[bool], shape: float,
                         level: float = 0.90) -> float:
    """Lower confidence bound on theta for an assumed shape (works with zero failures)."""
    t, d = _arrays(times, events)
    r = int(d.sum())
    chi2 = stats.chi2.ppf(level, 2 * r + 2)
    return float((2 * np.sum(t**shape) / chi2) ** (1 / shape))


@dataclass(frozen=True)
class LognormalFit:
    mu: float
    sigma: float
    n: int
    failures: int
    loglik: float

    def reliability(self, t):
        return stats.norm.sf((np.log(np.asarray(t, float)) - self.mu) / self.sigma)

    @property
    def mttf(self) -> float:
        return float(np.exp(self.mu + self.sigma**2 / 2))

    def quantile(self, p: float) -> float:
        return float(np.exp(self.mu + self.sigma * stats.norm.ppf(p)))


def fit_lognormal_mle(times: Sequence[float], events: Sequence[bool]) -> LognormalFit:
    t, d = _arrays(times, events)
    if d.sum() < 2:
        raise ValueError("the lognormal fit needs at least two failures")
    y = np.log(t)

    def negll(x: np.ndarray) -> float:
        mu, sigma = x[0], np.exp(x[1])
        z = (y - mu) / sigma
        logf = stats.norm.logpdf(z) - np.log(sigma) - y
        return -float(np.sum(np.where(d, logf, stats.norm.logsf(z))))

    x0 = np.array([y[d].mean(), np.log(max(y[d].std(), 0.1))])
    res = optimize.minimize(negll, x0, method="Nelder-Mead",
                            options={"xatol": 1e-9, "fatol": 1e-10, "maxiter": 4000})
    return LognormalFit(float(res.x[0]), float(np.exp(res.x[1])), len(t), int(d.sum()),
                        -float(res.fun))


def lr_test_common_shape(groups: Mapping[str, tuple[Sequence[float], Sequence[bool]]]
                         ) -> dict[str, float]:
    """Likelihood-ratio test of a common Weibull shape across groups.

    A common shape with group-specific scales is the usual assumption that
    lets small groups borrow strength from each other (and the assumption
    behind the regression model). Returns the statistic, df and p-value.
    """
    data = {g: _arrays(*v) for g, v in groups.items()}
    thin = [g for g, (_, d) in data.items() if d.sum() < 2]
    if thin or len(data) < 2:
        raise ValueError(f"need >= 2 groups with >= 2 failures each (too few: {thin})")
    separate = sum(fit_weibull_mle(t, d).loglik for t, d in data.values())

    def common_negll(log_g: float) -> float:
        g = np.exp(log_g)
        total = 0.0
        for t, d in data.values():
            scale = (np.sum(t**g) / d.sum()) ** (1 / g)
            total += weibull_loglik(t, d, g, scale)
        return -total

    res = optimize.minimize_scalar(common_negll, bounds=(-5, 5), method="bounded")
    stat = max(0.0, 2 * (separate + res.fun))
    df = len(data) - 1
    return {"statistic": stat, "df": df, "p_value": float(stats.chi2.sf(stat, df)),
            "common_shape": float(np.exp(res.x))}
