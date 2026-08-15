"""Small, dependency-free latency and transcript utilities for local evidence."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping


def percentile(values: Iterable[float], percentile_value: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("at least one sample is required")
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile_value / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def latency_summary(samples_ms: Iterable[float], *, environment: str) -> dict[str, Any]:
    values = [float(value) for value in samples_ms]
    if not values:
        raise ValueError("at least one latency sample is required")
    return {
        "environment": environment,
        "sample_count": len(values),
        "unit": "ms",
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "max": round(max(values), 3),
        "min": round(min(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "samples_ms": [round(value, 3) for value in values],
    }


def save_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return destination


__all__ = ["latency_summary", "percentile", "save_json"]
