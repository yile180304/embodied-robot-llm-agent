"""Strict simulation-ground-truth semantic query results."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import SemanticQueryColor, SemanticQueryKind
from .world import WorldConfig


class SemanticQueryFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SemanticQueryKind | None = None
    color: SemanticQueryColor | None = None
    label: str | None = None
    max_results: int = Field(default=8, ge=1, le=8)


class SemanticObjectEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    id: str
    kind: SemanticQueryKind
    label: str
    color: SemanticQueryColor
    distance_m: float = Field(..., ge=0.0)
    bearing_deg: float = Field(..., ge=-180.0, le=180.0)
    interaction_radius_m: float = Field(..., gt=0.0)
    blocking: bool
    within_interaction_radius: bool


class SemanticQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["simulation_ground_truth"] = "simulation_ground_truth"
    query: SemanticQueryFilter
    objects: tuple[SemanticObjectEvidence, ...]


def query_semantic_world(
    config: WorldConfig,
    *,
    x_m: float,
    y_m: float,
    yaw_deg: float,
    kind: SemanticQueryKind | None = None,
    color: SemanticQueryColor | None = None,
    label: str | None = None,
    max_results: int = 8,
) -> SemanticQueryResult:
    normalized_label = label.strip().casefold() if label is not None else None
    evidence: list[SemanticObjectEvidence] = []
    for item in config.semantic_objects:
        if kind is not None and item.kind != kind:
            continue
        if color is not None and item.color != color:
            continue
        if normalized_label is not None and normalized_label not in item.label.casefold():
            continue
        dx = item.center_x_m - x_m
        dy = item.center_y_m - y_m
        distance = math.hypot(dx, dy)
        absolute_bearing = math.degrees(math.atan2(dy, dx))
        relative_bearing = _normalize_bearing(absolute_bearing - yaw_deg)
        evidence.append(
            SemanticObjectEvidence(
                id=item.id,
                kind=item.kind,
                label=item.label,
                color=item.color,
                distance_m=round(distance, 6),
                bearing_deg=round(relative_bearing, 6),
                interaction_radius_m=item.interaction_radius_m,
                blocking=item.blocking,
                within_interaction_radius=distance <= item.interaction_radius_m + 1e-9,
            )
        )
    evidence.sort(key=lambda item: (item.distance_m, item.id))
    query = SemanticQueryFilter(
        kind=kind,
        color=color,
        label=label.strip() if label is not None else None,
        max_results=max_results,
    )
    return SemanticQueryResult(query=query, objects=tuple(evidence[:max_results]))


def _normalize_bearing(value: float) -> float:
    normalized = (value + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(normalized, -180.0, abs_tol=1e-12) and value > 0 else normalized


__all__ = [
    "SemanticObjectEvidence",
    "SemanticQueryFilter",
    "SemanticQueryResult",
    "query_semantic_world",
]
