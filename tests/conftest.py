from __future__ import annotations

import pytest

from embodied_agent.schemas import RobotState


@pytest.fixture
def clear_state() -> RobotState:
    return RobotState(
        front_distance_cm=100.0,
        left_distance_cm=120.0,
        right_distance_cm=80.0,
    )
