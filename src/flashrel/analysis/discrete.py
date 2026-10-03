"""Discrete-time proportional hazards for per-cycle failure records.

Every cycle of every drive is one Bernoulli trial: did the cycle fail? With
the complementary log-log link

    P(cycle k of drive i fails | it started) = 1 - exp(-exp(x_ik' beta)),

the coefficients are log hazard ratios of the continuous-time proportional
hazards model, so the workload of each cycle can enter as a *time-varying*
covariate (the same structure as the course's PH model with covariates
z(t)). Because every drive runs all three workloads, the workload effect is
estimated within drives, free of drive-to-drive differences in quality.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import optimize, stats

from flashrel.analysis.parametric import _numeric_hessian
from flashrel.analysis.regression import factor_labels


@dataclass(frozen=True)
class CloglogFit:
    names: tuple[str, ...]
    beta: np.ndarray
    cov: np.ndarray
    loglik: float
    trials: int
    events: int

    def table(self, level: float = 0.90) -> pd.DataFrame:
        z = stats.norm.ppf(0.5 + level / 2)
        se = np.sqrt(np.abs(np.diag(self.cov)))
        with np.errstate(over="ignore"):
            return pd.DataFrame({
                "term": self.names, "coef": self.beta, "se": se,
                "hazard_ratio": np.exp(self.beta), "hr_lo": np.exp(self.beta - z * se),
                "hr_hi": np.exp(self.beta + z * se),
                "p_value": 2 * stats.norm.sf(np.abs(self.beta / np.where(se > 0, se, np.nan))),
            })


def fit_cloglog(x: np.ndarray, y: Sequence[int], names: Sequence[str]) -> CloglogFit:
    """Maximum-likelihood fit of the complementary log-log model."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if y.sum() < 1:
        raise ValueError("no failed cycles: nothing to model")

    def negll(beta: np.ndarray) -> float:
        eta = np.clip(x @ beta, -30, 5)
        log_surv = -np.exp(eta)                 # ln P(no failure)
        log_fail = np.log(-np.expm1(log_surv))  # ln P(failure)
        return -float(np.sum(y * log_fail + (1 - y) * log_surv))

    beta0 = np.zeros(x.shape[1])
    beta0[0] = np.log(-np.log1p(-max(y.mean(), 1e-6)))
    res = optimize.minimize(negll, beta0, method="BFGS", options={"gtol": 1e-8, "maxiter": 5000})
    cov = np.linalg.pinv(_numeric_hessian(negll, res.x))
    return CloglogFit(tuple(names), res.x, cov, -float(res.fun), len(y), int(y.sum()))


def per_cycle_design(cycles: pd.DataFrame, reference_workload: str = "medium",
                     covariates: Sequence[str] = ()) -> tuple[np.ndarray, np.ndarray, tuple]:
    """Design matrix: intercept, ln(cycle), workload dummies, optional drive covariates."""
    df = cycles[cycles["outcome"] != "aborted"]
    y = (df["outcome"] != "pass").to_numpy(int)
    cols = [np.ones(len(df)), np.log(df["cycle"].to_numpy(float))]
    names = ["intercept", "ln(cycle)"]
    for level in sorted(df["workload"].unique()):
        if level != reference_workload:
            cols.append((df["workload"] == level).to_numpy(float))
            names.append(f"workload[{level}]")
    for cov in covariates:
        labels, levels = factor_labels(df[cov])
        for level in levels[1:]:
            cols.append((labels == level).to_numpy(float))
            names.append(f"{cov}[{level}]")
    return np.column_stack(cols), y, tuple(names)
