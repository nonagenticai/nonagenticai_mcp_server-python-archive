"""Statistics via scientific-processor (/api/stats/*): research_stats_meta_analysis,
research_stats_confidence_interval, research_stats_trend.

Pure computations over the numbers the caller passes; nothing is stored. Inputs are checked here
against the service's own limits (src/models/statistics.py) so a bad call fails fast as
"[permanent] ...", and the answer echoes `inputs_count` and the method so a reviewer can tie a
reported number to the call that produced it.
"""

from __future__ import annotations

import math
from typing import Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from ._server import call_json, research_mcp

SP = "scientific_processor"
MAX_STUDIES = 1000
MAX_VALUES = 100_000
MAX_POINTS = 10_000


class Study(BaseModel):
    effect_size: float
    standard_error: float
    sample_size: int
    label: str | None = None


class Point(BaseModel):
    timestamp: float
    value: float


def _finite(name: str, value: float) -> float:
    if not math.isfinite(value):
        raise ToolError(f"[permanent] {name} must be a finite number")
    return value


def _level(confidence_level: float) -> float:
    if not 0.5 <= confidence_level < 1.0:
        raise ToolError("[permanent] confidence_level must be in [0.5, 1.0)")
    return confidence_level


@research_mcp.tool()
async def stats_meta_analysis(
    studies: list[Study],
    method: Literal["fixed", "random"] = "random",
    confidence_level: float = 0.95,
) -> dict:
    """Inverse-variance meta-analysis (fixed or random effects) over 2+ study results, each an
    effect size with its standard error and sample size. Returns the pooled effect, its CI,
    heterogeneity (Q, I^2) and each study's weight (same order as given)."""
    if not 2 <= len(studies) <= MAX_STUDIES:
        raise ToolError(f"[permanent] studies must have 2-{MAX_STUDIES} entries")
    payload = []
    for i, s in enumerate(studies):
        _finite(f"studies[{i}].effect_size", s.effect_size)
        if not (_finite(f"studies[{i}].standard_error", s.standard_error) > 0):
            raise ToolError(f"[permanent] studies[{i}].standard_error must be > 0")
        if s.sample_size <= 0:
            raise ToolError(f"[permanent] studies[{i}].sample_size must be > 0")
        payload.append(s.model_dump(exclude_none=True))
    body = {"studies": payload, "method": method, "confidence_level": _level(confidence_level)}
    r = await call_json(SP, "POST", "/api/stats/meta-analysis", body=body)
    return {
        "method": r.get("method", method),
        "confidence_level": confidence_level,
        "studies_count": len(studies),
        "pooled_effect": r.get("pooled_effect"),
        "pooled_se": r.get("pooled_se"),
        "ci_lower": r.get("ci_lower"),
        "ci_upper": r.get("ci_upper"),
        "heterogeneity_q": r.get("heterogeneity_q"),
        "heterogeneity_i_squared": r.get("heterogeneity_i_squared"),
        "study_weights": [
            {"label": s.label, "weight": w}
            for s, w in zip(studies, r.get("study_weights") or [], strict=False)
        ],
    }


@research_mcp.tool()
async def stats_confidence_interval(
    values: list[float],
    method: Literal["normal", "bootstrap"] = "normal",
    confidence_level: float = 0.95,
    n_bootstrap: int = 10000,
) -> dict:
    """Mean of 2+ values with its confidence interval (normal approximation, or percentile
    bootstrap with n_bootstrap resamples, 100-100000)."""
    if not 2 <= len(values) <= MAX_VALUES:
        raise ToolError(f"[permanent] values must have 2-{MAX_VALUES} entries")
    for i, v in enumerate(values):
        _finite(f"values[{i}]", v)
    if not 100 <= n_bootstrap <= 100_000:
        raise ToolError("[permanent] n_bootstrap must be 100-100000")
    body = {
        "values": values,
        "method": method,
        "confidence_level": _level(confidence_level),
        "n_bootstrap": n_bootstrap,
    }
    r = await call_json(SP, "POST", "/api/stats/confidence-interval", body=body)
    return {
        "method": r.get("method", method),
        "confidence_level": r.get("confidence_level", confidence_level),
        "values_count": len(values),
        "mean": r.get("mean"),
        "ci_lower": r.get("ci_lower"),
        "ci_upper": r.get("ci_upper"),
    }


@research_mcp.tool()
async def stats_trend(
    data: list[Point],
    test: Literal["mann_kendall", "linear_regression"] = "mann_kendall",
) -> dict:
    """Trend test over a time series of 3+ (timestamp, value) points: Mann-Kendall (monotonic,
    non-parametric) or linear regression (slope + intercept). Returns increasing / decreasing /
    no_trend with the p-value and test statistic."""
    if not 3 <= len(data) <= MAX_POINTS:
        raise ToolError(f"[permanent] data must have 3-{MAX_POINTS} points")
    for i, p in enumerate(data):
        _finite(f"data[{i}].timestamp", p.timestamp)
        _finite(f"data[{i}].value", p.value)
    body = {"data": [p.model_dump() for p in data], "test": test}
    r = await call_json(SP, "POST", "/api/stats/trend", body=body)
    return {
        "test": test,
        "points_count": len(data),
        "trend": r.get("trend"),
        "p_value": r.get("p_value"),
        "statistic": r.get("statistic"),
        "slope": r.get("slope"),
        "intercept": r.get("intercept"),
    }
