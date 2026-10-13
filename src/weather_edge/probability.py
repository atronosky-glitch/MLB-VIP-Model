"""Outcome probabilities from an ensemble forecast (pure, no I/O).

Method ("ensemble dressing", a standard post-processing step):

1. Each ensemble member gives one forecast of the day's extreme temperature.
2. The members are shifted by the station/lead-time BIAS and smoothed with a Gaussian kernel,
   producing a continuous predictive distribution (the average of one normal per member).
3. The kernel width is chosen so the predictive spread matches the empirically measured forecast
   error (``residual_std``) when the raw ensemble is under-dispersed, with a small floor so the
   distribution never collapses to spikes. If the raw ensemble is already wider than the measured
   error it is kept as is (conservative: never narrower than the data supports).
4. Contracts settle on a reported WHOLE-degree value, so a bin ``[lo, hi]`` is the probability that
   the reported integer lands in it: ``CDF(hi + 0.5) - CDF(lo - 0.5)`` (rounding to nearest).

Probabilities for every contract of one event come from the SAME distribution, so they are
coherent by construction: the outcomes of a complete partition sum to 1 (see ``check_partition``).
These are modeled estimates, not guarantees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Bin

_SQRT2 = math.sqrt(2.0)
PROBABILITY_FLOOR = 0.01     # a forecast model is never certain: clip so a 0/1 never prices a "sure thing"
PROBABILITY_CEIL = 0.99
DEFAULT_SIGMA_FLOOR = 0.75   # °F


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


@dataclass(frozen=True)
class DressedDistribution:
    centers: tuple[float, ...]
    sigma: float

    def cdf(self, x: float) -> float:
        if not self.centers:
            raise ValueError("empty distribution")
        return sum(_phi((x - c) / self.sigma) for c in self.centers) / len(self.centers)

    @property
    def mean(self) -> float:
        return sum(self.centers) / len(self.centers)

    @property
    def std(self) -> float:
        n = len(self.centers)
        m = self.mean
        var_centers = sum((c - m) ** 2 for c in self.centers) / n
        return math.sqrt(var_centers + self.sigma ** 2)


def _pstdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def build_distribution(
    member_values: list[float], bias: float, residual_std: float, sigma_floor: float = DEFAULT_SIGMA_FLOOR,
) -> DressedDistribution:
    """Dress ``member_values`` (one daily-extreme forecast per member, °F)."""
    if not member_values:
        raise ValueError("no ensemble members")
    if residual_std <= 0 or sigma_floor <= 0:
        raise ValueError("residual_std and sigma_floor must be positive")
    spread = _pstdev(member_values)
    kernel_var = max(residual_std ** 2 - spread ** 2, sigma_floor ** 2)
    return DressedDistribution(tuple(v + bias for v in member_values), math.sqrt(kernel_var))


def point_distribution(point: float, std: float) -> DressedDistribution:
    """A single deterministic forecast with a fixed spread: the simple BASELINE the model is
    compared against."""
    if std <= 0:
        raise ValueError("std must be positive")
    return DressedDistribution((point,), std)


def bin_probability(dist: DressedDistribution, b: Bin) -> float:
    upper = 1.0 if b.hi is None else dist.cdf(b.hi + 0.5)
    lower = 0.0 if b.lo is None else dist.cdf(b.lo - 0.5)
    return max(0.0, min(1.0, upper - lower))


def clip_probability(p: float, lo: float = PROBABILITY_FLOOR, hi: float = PROBABILITY_CEIL) -> float:
    return max(lo, min(hi, p))


def probability_interval(
    member_values: list[float], bias: float, residual_std: float, bias_se: float, b: Bin,
    sigma_floor: float = DEFAULT_SIGMA_FLOOR, std_rel_error: float = 0.15,
) -> tuple[float, float]:
    """(low, high) probability when the calibration is wrong by about one standard error.

    Re-evaluates the bin with the bias shifted by +/- ``bias_se`` and the error std scaled by
    (1 -/+ ``std_rel_error``). The spread of results is the model's own parameter uncertainty and is
    what separates a robust edge from one that exists only at the point estimate.
    """
    probs = []
    for db in (-bias_se, 0.0, bias_se):
        for scale in (1.0 - std_rel_error, 1.0, 1.0 + std_rel_error):
            d = build_distribution(member_values, bias + db, residual_std * scale, sigma_floor)
            probs.append(bin_probability(d, b))
    return min(probs), max(probs)


def check_partition(bins: list[Bin]) -> tuple[bool, list[str]]:
    """True when ``bins`` cover every integer exactly once (so outcome probabilities sum to 1)."""
    problems: list[str] = []
    if not bins:
        return False, ["no bins"]
    ordered = sorted(bins, key=lambda b: (-10 ** 6 if b.lo is None else b.lo))
    if ordered[0].lo is not None:
        problems.append(f"nothing covers values below {ordered[0].lo}")
    if ordered[-1].hi is not None:
        problems.append(f"nothing covers values above {ordered[-1].hi}")
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.hi is None or nxt.lo is None:
            problems.append(f"unbounded bin overlaps its neighbour ({prev.label} / {nxt.label})")
        elif nxt.lo <= prev.hi:
            problems.append(f"overlap between {prev.label} and {nxt.label}")
        elif nxt.lo > prev.hi + 1:
            problems.append(f"gap between {prev.label} and {nxt.label}")
    return (not problems), problems


def apply_observed_extreme(values: list[float], observed: float | None, metric: str) -> list[float]:
    """Same-day markets: the part of the day that already happened is KNOWN, so each member's
    daily extreme can never be lower than the observed high (or higher than the observed low)."""
    if observed is None:
        return list(values)
    if metric == "high":
        return [max(v, observed) for v in values]
    if metric == "low":
        return [min(v, observed) for v in values]
    raise ValueError(f"unsupported metric {metric!r}")
