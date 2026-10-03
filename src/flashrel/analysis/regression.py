"""Weibull regression: how brand, capacity and workload shift drive life.

The model is the accelerated-failure-time form of the Weibull distribution,

    ln T = x' beta + sigma * W,    W ~ standard smallest-extreme-value,

so every covariate multiplies the characteristic life by exp(beta_j) and the
shape gamma = 1/sigma is common to all drives. The same model is a
proportional-hazards model with hazard ratio exp(-beta_j / sigma), which links
it to the PH model of the course. A common shape lets nine drives in three
groups share one estimate of gamma instead of three noisy ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import optimize, stats

from flashrel.analysis.parametric import _arrays, _numeric_hessian


@dataclass(frozen=True)
class WeibullRegression:
    names: tuple[str, ...]
    beta: np.ndarray
    sigma: float
    cov: np.ndarray  # covariance of (beta..., ln sigma)
    loglik: float
    n: int
    failures: int

    @property
    def shape(self) -> float:
        return 1.0 / self.sigma

    def scale_for(self, x: Sequence[float]) -> float:
        """Characteristic life theta for a covariate row (including the intercept)."""
        return float(np.exp(np.asarray(x, float) @ self.beta))

    def table(self, level: float = 0.90) -> pd.DataFrame:
        """Coefficients with Wald intervals; few failures can make an interval unbounded."""
        z = stats.norm.ppf(0.5 + level / 2)
        se = np.sqrt(np.abs(np.diag(self.cov)))[: len(self.beta)]
        rows = []
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            for name, b, s in zip(self.names, self.beta, se):
                rows.append({
                    "term": name, "coef": b, "se": s, "z": b / s if s > 0 else np.nan,
                    "p_value": 2 * stats.norm.sf(abs(b / s)) if s > 0 else np.nan,
                    "life_ratio": np.exp(b), "life_ratio_lo": np.exp(b - z * s),
                    "life_ratio_hi": np.exp(b + z * s),
                    "hazard_ratio": np.exp(-b / self.sigma),
                })
        return pd.DataFrame(rows)


def factor_labels(values: pd.Series) -> tuple[pd.Series, list[str]]:
    """Readable labels (8.0 -> "8") and their levels in natural order (8 before 16)."""
    def label(v) -> str:
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)

    labels = values.map(label)
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().all():
        order = sorted(set(zip(numeric, labels)))
        levels = list(dict.fromkeys(lbl for _, lbl in order))
    else:
        levels = sorted(labels.unique())
    return labels, levels


def design_matrix(frame: pd.DataFrame, factors: Sequence[str],
                  reference: dict[str, str] | None = None) -> tuple[np.ndarray, tuple[str, ...]]:
    """Intercept plus treatment-coded dummies (first level, or ``reference``, as baseline)."""
    reference = reference or {}
    columns, names = [np.ones(len(frame))], ["intercept"]
    for factor in factors:
        labels, levels = factor_labels(frame[factor])
        base = reference.get(factor, levels[0])
        for level in levels:
            if level != base:
                columns.append((labels == level).to_numpy(float))
                names.append(f"{factor}[{level}]")
    return np.column_stack(columns), tuple(names)


def fit_weibull_regression(times: Sequence[float], events: Sequence[bool], x: np.ndarray,
                           names: Sequence[str]) -> WeibullRegression:
    t, d = _arrays(times, events)
    x = np.asarray(x, float)
    if x.shape[0] != len(t):
        raise ValueError("design matrix and data have different lengths")
    if d.sum() < x.shape[1] + 1:
        raise ValueError(f"{int(d.sum())} failures cannot support {x.shape[1]} coefficients "
                         "plus a shape")
    y = np.log(t)

    def negll(p: np.ndarray) -> float:
        beta, sigma = p[:-1], np.exp(p[-1])
        w = (y - x @ beta) / sigma
        ew = np.exp(np.clip(w, -700, 700))
        return -float(np.sum(np.where(d, w - ew - np.log(sigma) - y, -ew)))

    beta0, *_ = np.linalg.lstsq(x, y, rcond=None)
    p0 = np.append(beta0, np.log(max(np.std(y - x @ beta0), 0.2)))
    res = optimize.minimize(negll, p0, method="BFGS", options={"gtol": 1e-8, "maxiter": 2000})
    hess = _numeric_hessian(negll, res.x)
    cov = np.linalg.pinv(hess)
    return WeibullRegression(tuple(names), res.x[:-1], float(np.exp(res.x[-1])), cov,
                             -float(res.fun), len(t), int(d.sum()))


def likelihood_ratio(full: WeibullRegression, reduced: WeibullRegression) -> dict[str, float]:
    """LR test that the extra terms of ``full`` are zero."""
    stat = max(0.0, 2 * (full.loglik - reduced.loglik))
    df = len(full.beta) - len(reduced.beta)
    return {"statistic": stat, "df": df, "p_value": float(stats.chi2.sf(stat, df))}
