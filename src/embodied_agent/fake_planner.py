"""Deterministic FakePlanner used by tests and local simulation demos."""

from __future__ import annotations

import math
import re

from .agent_graph import PlannerContext, PlannerDecision
from .schemas import ObservationMessage, ObservationStatus

class FakePlanner:
    """Deterministic planner for tests; every follow-up depends on Observation."""

    _MIN_DETOUR_CLEARANCE_M = 0.85
    _STOP_CLEARANCE_M = 0.25
    _MAX_LATERAL_DISTANCE_M = 1.2
    _DETOUR_CALLS_AFTER_BLOCKED = 5

    def __init__(self, *, linear_speed_mps: float = 0.2, turn_speed_dps: float = 45.0) -> None:
        self.linear_speed_mps = linear_speed_mps
        self.turn_speed_dps = turn_speed_dps

    def plan(self, context: PlannerContext) -> PlannerDecision:
        goal = context.goal.lower()
        semantic_targets = self._semantic_targets(goal)
        if semantic_targets is not None:
            return self._plan_semantic(context, semantic_targets)
        if not context.steps:
            if "急停" in goal or "emergency" in goal:
                return PlannerDecision.call(
                    "emergency_stop",
                    {"reason": "user requested emergency stop"},
                    "用户请求急停。",
                )
            if "状态" in goal or "state" in goal:
                return PlannerDecision.call("get_robot_state", {}, "读取当前机器人状态。")
            if "扫描" in goal or "scan" in goal:
                return PlannerDecision.call("scan_obstacles", {}, "扫描三个方向的障碍距离。")
            if "全速" in goal or "5 m/s" in goal or "5m/s" in goal:
                return PlannerDecision.call(
                    "move_robot",
                    {"distance_m": 10.0, "speed_mps": 5.0},
                    "按用户原始危险参数生成结构化调用，交给 Safety 拒绝。",
                )
            distance = self._distance_from_goal(context.goal)
            return PlannerDecision.call(
                "move_robot",
                {"distance_m": distance, "speed_mps": self.linear_speed_mps},
                f"尝试前进 {distance:g} 米。",
            )

        last_step = context.steps[-1]
        observation = last_step.observation
        if observation.status is ObservationStatus.REJECTED:
            return PlannerDecision.finish(
                "rejected",
                f"工具调用被安全层拒绝：{observation.error_code or 'unknown'}。",
            )
        if observation.status is ObservationStatus.TIMEOUT:
            return PlannerDecision.finish("timeout", "设备反馈超时，任务安全退出。")
        if observation.status is ObservationStatus.EMERGENCY_STOP:
            return PlannerDecision.finish("emergency_stop", "设备已进入急停状态。")
        if observation.status is ObservationStatus.BLOCKED:
            blocked_count = sum(
                step.observation.status is ObservationStatus.BLOCKED for step in context.steps
            )
            if blocked_count > 1:
                return PlannerDecision.finish(
                    "rejected",
                    "绕行过程中再次受阻，已停止继续尝试。",
                )
            if context.step_count + self._DETOUR_CALLS_AFTER_BLOCKED >= context.max_steps:
                return PlannerDecision.finish(
                    "step_limit",
                    "剩余步骤预算不足以完成一次有界绕行，任务安全退出。",
                )
            return PlannerDecision.call(
                "scan_obstacles",
                {},
                "前进受阻，先读取左右空间再决定转向。",
            )
        first_blocked_index = next(
            (
                index
                for index, step in enumerate(context.steps)
                if step.observation.status is ObservationStatus.BLOCKED
            ),
            None,
        )
        if first_blocked_index is not None:
            return self._plan_detour(context, first_blocked_index)
        if observation.status is ObservationStatus.SUCCESS:
            return PlannerDecision.finish("success", "任务已根据设备 Observation 完成。")
        return PlannerDecision.finish("rejected", "设备返回了无法处理的状态。")

    def _plan_detour(
        self,
        context: PlannerContext,
        blocked_index: int,
        *,
        resume_semantic: tuple[str, str] | None = None,
    ) -> PlannerDecision:
        blocked_step = context.steps[blocked_index]
        detour_steps = context.steps[blocked_index + 1 :]
        remaining = self._observation_number(
            blocked_step.observation,
            "remaining_distance_m",
        )
        if remaining is None or remaining <= 0.0:
            return PlannerDecision.finish("rejected", "受阻反馈缺少有效的剩余距离，任务安全退出。")
        if not detour_steps or detour_steps[0].tool_call.name != "scan_obstacles":
            return PlannerDecision.finish("rejected", "绕行序列缺少障碍扫描结果，任务安全退出。")

        scan = detour_steps[0].observation
        left_cm = self._observation_number(scan, "left_distance_cm")
        right_cm = self._observation_number(scan, "right_distance_cm")
        if left_cm is None or right_cm is None:
            return PlannerDecision.finish("rejected", "障碍扫描缺少左右距离，任务安全退出。")
        angle = 90.0 if left_cm >= right_cm else -90.0
        side = "左" if angle > 0 else "右"
        chosen_clearance_m = (left_cm if angle > 0 else right_cm) / 100.0
        if chosen_clearance_m < self._MIN_DETOUR_CLEARANCE_M:
            return PlannerDecision.finish(
                "rejected",
                f"{side}侧净空不足 0.85 米，不尝试擦边绕行。",
            )
        lateral_distance = min(
            self._MAX_LATERAL_DISTANCE_M,
            chosen_clearance_m - self._STOP_CLEARANCE_M,
        )

        if len(detour_steps) == 1:
            return PlannerDecision.call(
                "turn_robot",
                {"angle_deg": angle, "angular_speed_dps": self.turn_speed_dps},
                f"{side}侧空间更宽，向{side}转 90 度。",
            )
        if len(detour_steps) == 2:
            return PlannerDecision.call(
                "move_robot",
                {"distance_m": lateral_distance, "speed_mps": self.linear_speed_mps},
                f"沿{side}侧移动 {lateral_distance:g} 米建立绕行净空。",
            )
        if len(detour_steps) == 3:
            return PlannerDecision.call(
                "turn_robot",
                {"angle_deg": -angle, "angular_speed_dps": self.turn_speed_dps},
                "侧移完成，转回原始前进方向。",
            )
        if len(detour_steps) == 4:
            return PlannerDecision.call(
                "move_robot",
                {"distance_m": remaining, "speed_mps": self.linear_speed_mps},
                f"方向恢复，完成剩余 {remaining:g} 米。",
            )
        if len(detour_steps) == 5 and detour_steps[-1].observation.status is ObservationStatus.SUCCESS:
            if resume_semantic is not None:
                kind, color = resume_semantic
                return PlannerDecision.call(
                    "inspect_semantic_world",
                    {"kind": kind, "color": color, "max_results": 4},
                    "绕行完成，重新查询当前语义目标的到达距离。",
                )
            return PlannerDecision.finish("success", "一次有界绕行完成，剩余前进距离已执行。")
        return PlannerDecision.finish("rejected", "绕行序列进入无效状态，任务安全退出。")

    @staticmethod
    def _semantic_targets(goal: str) -> tuple[tuple[str, str], ...] | None:
        has_bottle = "瓶" in goal or "bottle" in goal
        has_goal_zone = "目标区" in goal or "goal zone" in goal
        if not (has_bottle and has_goal_zone):
            return None
        if not ("红" in goal or "red" in goal):
            return ()
        if not ("蓝" in goal or "blue" in goal):
            return ()
        return (("bottle", "red"), ("goal_zone", "blue"))

    def _plan_semantic(
        self,
        context: PlannerContext,
        targets: tuple[tuple[str, str], ...],
    ) -> PlannerDecision:
        if not targets:
            return PlannerDecision.finish("rejected", "语义任务只支持红色瓶子和蓝色目标区。")
        progress = self._semantic_progress(context, targets)
        if progress >= len(targets):
            return PlannerDecision.finish("success", "语义目标和蓝色目标区均已由仿真真值确认。")

        if context.steps and context.steps[-1].observation.status is ObservationStatus.EMERGENCY_STOP:
            return PlannerDecision.finish("emergency_stop", "设备已进入急停状态。")
        if context.steps and context.steps[-1].observation.status is ObservationStatus.TIMEOUT:
            return PlannerDecision.finish("timeout", "语义任务设备反馈超时，安全退出。")
        if context.steps and context.steps[-1].observation.status is ObservationStatus.REJECTED:
            return PlannerDecision.finish(
                "rejected",
                f"语义任务调用被拒绝：{context.steps[-1].observation.error_code or 'unknown'}。",
            )

        blocked_indexes = [
            index
            for index, step in enumerate(context.steps)
            if step.observation.status is ObservationStatus.BLOCKED
        ]
        if blocked_indexes:
            if len(blocked_indexes) > 1:
                return PlannerDecision.finish("rejected", "语义任务绕行中再次受阻，已停止继续尝试。")
            blocked_index = blocked_indexes[0]
            has_resume_query = any(
                step.tool_call.name == "inspect_semantic_world"
                for step in context.steps[blocked_index + 1 :]
            )
            if not has_resume_query:
                if context.step_count + self._DETOUR_CALLS_AFTER_BLOCKED >= context.max_steps:
                    return PlannerDecision.finish("step_limit", "剩余步骤预算不足以完成语义绕行。")
                return self._plan_detour(
                    context,
                    blocked_index,
                    resume_semantic=targets[progress],
                )

        if context.step_count >= context.max_steps - 1:
            return PlannerDecision.finish("step_limit", "语义任务达到有界步骤预算。")

        current_kind, current_color = targets[progress]
        last_step = context.steps[-1] if context.steps else None
        if last_step is None or last_step.tool_call.name != "inspect_semantic_world":
            return PlannerDecision.call(
                "inspect_semantic_world",
                {"kind": current_kind, "color": current_color, "max_results": 4},
                f"查询{current_color}语义目标的位置和到达距离。",
            )

        objects = last_step.observation.observation.get("objects")
        if not isinstance(objects, list) or not objects:
            return PlannerDecision.finish("rejected", f"仿真真值中没有找到 {current_color} {current_kind}。")
        target = objects[0]
        if not isinstance(target, dict):
            return PlannerDecision.finish("rejected", "语义查询返回了无法处理的对象证据。")
        distance = self._finite_observation_number(target, "distance_m")
        bearing = self._finite_observation_number(target, "bearing_deg")
        interaction_radius = self._finite_observation_number(target, "interaction_radius_m")
        if distance is None or bearing is None or interaction_radius is None:
            return PlannerDecision.finish("rejected", "语义查询缺少距离、方向或到达阈值。")
        if bool(target.get("within_interaction_radius")):
            return PlannerDecision.call(
                "inspect_semantic_world",
                {"kind": current_kind, "color": current_color, "max_results": 4},
                "再次确认当前语义目标的到达证据。",
            )
        if abs(bearing) > 1.0:
            angle = max(-180.0, min(180.0, bearing))
            return PlannerDecision.call(
                "turn_robot",
                {"angle_deg": angle, "angular_speed_dps": self.turn_speed_dps},
                f"向语义目标方向转动 {angle:g} 度。",
            )
        distance_to_target = distance - interaction_radius
        if distance_to_target <= 0.0:
            return PlannerDecision.finish("rejected", "语义查询距离与到达阈值不一致。")
        move_distance = min(2.0, distance_to_target)
        return PlannerDecision.call(
            "move_robot",
            {"distance_m": move_distance, "speed_mps": self.linear_speed_mps},
            f"向语义目标前进 {move_distance:g} 米。",
        )

    @staticmethod
    def _semantic_progress(context: PlannerContext, targets: tuple[tuple[str, str], ...]) -> int:
        progress = 0
        for step in context.steps:
            if step.tool_call.name != "inspect_semantic_world" or progress >= len(targets):
                continue
            kind, color = targets[progress]
            query = step.tool_call.arguments
            if query.get("kind") != kind or query.get("color") != color:
                continue
            objects = step.observation.observation.get("objects")
            if not isinstance(objects, list):
                continue
            if any(
                isinstance(item, dict)
                and item.get("kind") == kind
                and item.get("color") == color
                and item.get("within_interaction_radius") is True
                for item in objects
            ):
                progress += 1
        return progress

    @staticmethod
    def _finite_observation_number(value: object, name: str) -> float | None:
        if not isinstance(value, dict):
            return None
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        number = float(raw)
        return number if math.isfinite(number) else None

    @staticmethod
    def _observation_number(observation: ObservationMessage, name: str) -> float | None:
        value = observation.observation.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    @staticmethod
    def _distance_from_goal(goal: str) -> float:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:米|m\b)", goal, flags=re.IGNORECASE)
        if match:
            return min(float(match.group(1)), 2.0)
        return 1.0
