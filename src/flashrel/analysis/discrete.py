"""Discrete-time proportional hazards for per-cycle failure records.

Every cycle of every drive is one Bernoulli trial: did the cycle fail? With
the complementary log-log link

    P(cycle k of drive i fails | it started) = 1 - exp(-exp(x_ik' beta)),

the coefficients are log hazard ratios of the continuous-time proportional
hazards model, so the workload of each cycle can enter as a *time-varying*
covariate (the same structure as the course's PH model with covariates
z(t)). Because every drive runs all three workloads, the workload effect is
estimated within drives, free of drive-to-drive differences in quality.
Cycles of one drive are not independent, so standard errors are clustered by
drive (sandwich estimator).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import optimize, stats

from flashrel.analysis.parametric import _numeric_hessian
from flashrel.analysis.regression import factor_labels

#: Rows that are not trials of the drive: stopped cycles and port faults (F5).
NOT_TRIALS = ("aborted", "port_fault")


@dataclass(frozen=True)
class CloglogFit:
    names: tuple[str, ...]
    beta: np.ndarray
    cov: np.ndarray
    loglik: float
    trials: int
    events: int
    robust: bool

    def table(self, level: float = 0.90) -> pd.DataFrame:
        z = stats.norm.ppf(0.5 + level / 2)
        se = np.sqrt(np.abs(np.diag(self.cov)))
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            return pd.DataFrame({
                "term": self.names, "coef": self.beta, "se": se,
                "hazard_ratio": np.exp(self.beta), "hr_lo": np.exp(self.beta - z * se),
                "hr_hi": np.exp(self.beta + z * se),
                "p_value": 2 * stats.norm.sf(np.abs(self.beta / np.where(se > 0, se, np.nan))),
                "se_type": "cluster-robust" if self.robust else "model",
            })


def _scores(x: np.ndarray, y: np.ndarray, beta: np.ndarray) -> np.ndarray:
    """Per-trial score vectors of the cloglog log-likelihood."""
    mu = np.exp(np.clip(x @ beta, -30, 5))
    weight = np.where(y > 0, mu / np.expm1(mu), -mu)
    return x * weight[:, None]


def fit_cloglog(x: np.ndarray, y: Sequence[int], names: Sequence[str],
                clusters: Sequence | None = None) -> CloglogFit:
    """Maximum-likelihood fit; with ``clusters`` the covariance is cluster-robust."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if y.sum() < 1:
        raise ValueError("no failed cycles: nothing to model")
    if np.linalg.matrix_rank(x) < x.shape[1]:
        raise ValueError("design matrix is rank-deficient (a level has no cycles)")

    def negll(beta: np.ndarray) -> float:
        eta = np.clip(x @ beta, -30, 5)
        log_surv = -np.exp(eta)                 # ln P(no failure)
        log_fail = np.log(-np.expm1(log_surv))  # ln P(failure)
        return -float(np.sum(y * log_fail + (1 - y) * log_surv))

    beta0 = np.zeros(x.shape[1])
    beta0[0] = np.log(-np.log1p(-max(y.mean(), 1e-6)))
    res = optimize.minimize(negll, beta0, method="BFGS", options={"gtol": 1e-8, "maxiter": 5000})
    bread = np.linalg.pinv(_numeric_hessian(negll, res.x))
    cov, robust = bread, False
    if clusters is not None:
        scores = pd.DataFrame(_scores(x, y, res.x)).groupby(np.asarray(clusters)).sum()
        meat = scores.to_numpy().T @ scores.to_numpy()
        cov, robust = bread @ meat @ bread, True
    return CloglogFit(tuple(names), res.x, cov, -float(res.fun), len(y), int(y.sum()), robust)


def per_cycle_design(cycles: pd.DataFrame, reference_workload: str = "medium",
                     covariates: Sequence[str] = ()):
    """Design matrix, outcomes, term names and drive clusters for :func:`fit_cloglog`.

    Columns: intercept, ln(cycle), workload dummies (``reference_workload`` as
    baseline, or the most common workload if it is absent), drive covariates.
    """
    df = cycles[~cycles["outcome"].isin(NOT_TRIALS)]
    y = (df["outcome"] != "pass").to_numpy(int)
    cols = [np.ones(len(df)), np.log(df["cycle"].to_numpy(float))]
    names = ["intercept", "ln(cycle)"]
    workloads = df["workload"].value_counts()
    reference = reference_workload if reference_workload in workloads else workloads.index[0]
    for level in sorted(workloads.index):
        if level != reference:
            cols.append((df["workload"] == level).to_numpy(float))
            names.append(f"workload[{level}]")
    for cov in covariates:
        labels, levels = factor_labels(df[cov])
        for level in levels[1:]:
            cols.append((labels == level).to_numpy(float))
            names.append(f"{cov}[{level}]")
    return np.column_stack(cols), y, tuple(names), df["drive_id"].to_numpy()
