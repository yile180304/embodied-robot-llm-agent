export type ConnectionState = "connecting" | "live" | "reconnecting" | "offline";

export type SimulationGait = "stand" | "walk" | "turn" | "stopped";

export type SimulationRobotState = {
  x_m: number;
  y_m: number;
  yaw_deg: number;
  linear_speed_mps: number;
  angular_speed_dps: number;
  gait: SimulationGait;
  gait_phase: number;
  emergency_stopped: boolean;
};

export type SimulationSensors = {
  front_distance_cm: number;
  left_distance_cm: number;
  right_distance_cm: number;
};

export type ActiveSimulationCommand = {
  task_id: string;
  seq: number;
  tool: "move_robot" | "turn_robot";
  progress: number;
};

export type SimulationFrame = {
  type: "simulation.frame";
  version: 1;
  revision: number;
  sim_time_ms: number;
  robot: SimulationRobotState;
  sensors: SimulationSensors;
  active_command: ActiveSimulationCommand | null;
};

export type SimulationViewState = {
  connection: ConnectionState;
  latestFrame: SimulationFrame | null;
  previousFrame: SimulationFrame | null;
};

export const initialSimulationViewState: SimulationViewState = {
  connection: "connecting",
  latestFrame: null,
  previousFrame: null,
};

const GAITS = new Set<SimulationGait>(["stand", "walk", "turn", "stopped"]);
const TOOLS = new Set<ActiveSimulationCommand["tool"]>(["move_robot", "turn_robot"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isRobotState(value: unknown): value is SimulationRobotState {
  if (!isRecord(value)) return false;
  return (
    isFiniteNumber(value.x_m)
    && isFiniteNumber(value.y_m)
    && isFiniteNumber(value.yaw_deg)
    && isFiniteNumber(value.linear_speed_mps)
    && isFiniteNumber(value.angular_speed_dps)
    && typeof value.gait === "string"
    && GAITS.has(value.gait as SimulationGait)
    && isFiniteNumber(value.gait_phase)
    && value.gait_phase >= 0
    && value.gait_phase < 1
    && typeof value.emergency_stopped === "boolean"
  );
}

function isSensors(value: unknown): value is SimulationSensors {
  if (!isRecord(value)) return false;
  return (
    isFiniteNumber(value.front_distance_cm)
    && value.front_distance_cm >= 0
    && isFiniteNumber(value.left_distance_cm)
    && value.left_distance_cm >= 0
    && isFiniteNumber(value.right_distance_cm)
    && value.right_distance_cm >= 0
  );
}

function isActiveCommand(value: unknown): value is ActiveSimulationCommand | null {
  if (value === null) return true;
  if (!isRecord(value)) return false;
  return (
    typeof value.task_id === "string"
    && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/.test(value.task_id)
    && Number.isInteger(value.seq)
    && Number(value.seq) >= 1
    && typeof value.tool === "string"
    && TOOLS.has(value.tool as ActiveSimulationCommand["tool"])
    && isFiniteNumber(value.progress)
    && value.progress >= 0
    && value.progress <= 1
  );
}

export function parseSimulationFrame(value: unknown): SimulationFrame | null {
  if (!isRecord(value)) return null;
  if (
    value.type !== "simulation.frame"
    || value.version !== 1
    || !Number.isInteger(value.revision)
    || Number(value.revision) < 0
    || !Number.isInteger(value.sim_time_ms)
    || Number(value.sim_time_ms) < 0
    || !isRobotState(value.robot)
    || !isSensors(value.sensors)
    || !isActiveCommand(value.active_command)
  ) {
    return null;
  }
  return value as SimulationFrame;
}

export function acceptSimulationFrame(
  state: SimulationViewState,
  frame: SimulationFrame,
): SimulationViewState {
  if (state.latestFrame && frame.revision <= state.latestFrame.revision) return state;
  return {
    ...state,
    previousFrame: state.latestFrame,
    latestFrame: frame,
  };
}

export function toThreePose(frame: SimulationFrame) {
  return {
    x: frame.robot.x_m,
    z: frame.robot.y_m,
    yawRadians: -frame.robot.yaw_deg * Math.PI / 180,
  };
}
