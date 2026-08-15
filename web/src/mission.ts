export type PlannerMode = "fake" | "model";
export type ToolName =
  | "move_robot"
  | "turn_robot"
  | "get_robot_state"
  | "scan_obstacles"
  | "inspect_semantic_world"
  | "emergency_stop";
export type RuntimeEventPhase =
  | "user_goal"
  | "planning"
  | "tool_call"
  | "published"
  | "observation"
  | "replanning"
  | "final"
  | "safety_rejected";

export type RuntimeEvent = {
  type: "agent.step";
  version: 1;
  event_id: string;
  timestamp_ms: number;
  task_id: string;
  seq: number;
  phase: RuntimeEventPhase;
  tool: ToolName | null;
  payload: Record<string, unknown>;
};

export type RuntimeCapabilities = {
  version: 1;
  mode: "preview" | "bridge";
  device_id: string | null;
  mqtt_endpoint: string | null;
  fake_planner: true;
  model_configured: boolean;
  fault_injection: boolean;
};

export type MissionTaskSnapshot = {
  version: 1;
  task_id: string;
  goal: string;
  planner: PlannerMode;
  max_steps: number;
  status: "accepted" | "running" | "finished";
  accepted_at_ms: number;
  started_at_ms: number | null;
  finished_at_ms: number | null;
  final_status: string | null;
  final_message: string | null;
  events: RuntimeEvent[];
};

export type MissionTaskRequest = {
  goal: string;
  planner: PlannerMode;
  max_steps: number;
};

export type RuntimeApiError = {
  version: 1;
  error_code: string;
  error_message: string;
};

const TOOL_NAMES = new Set<ToolName>([
  "move_robot",
  "turn_robot",
  "get_robot_state",
  "scan_obstacles",
  "inspect_semantic_world",
  "emergency_stop",
]);
const PHASES = new Set<RuntimeEventPhase>([
  "user_goal",
  "planning",
  "tool_call",
  "published",
  "observation",
  "replanning",
  "final",
  "safety_rejected",
]);
const TOOL_PHASES = new Set<RuntimeEventPhase>([
  "tool_call",
  "published",
  "observation",
  "safety_rejected",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const expected = new Set(keys);
  return Object.keys(value).length === keys.length
    && Object.keys(value).every((key) => expected.has(key));
}

function isIdentifier(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/.test(value);
}

function isInteger(value: unknown, minimum = 0): value is number {
  return Number.isInteger(value) && Number(value) >= minimum;
}

function isToolName(value: unknown): value is ToolName {
  return typeof value === "string" && TOOL_NAMES.has(value as ToolName);
}

function isString(value: unknown, maxLength = 512): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength;
}

function isCommand(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return value.version === 1
    && isIdentifier(value.task_id)
    && isInteger(value.seq, 1)
    && isToolName(value.tool)
    && isRecord(value.params)
    && isInteger(value.deadline_ms, 1)
    && isInteger(value.sent_at_ms);
}

function isObservation(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return value.version === 1
    && isIdentifier(value.task_id)
    && isInteger(value.seq, 1)
    && typeof value.status === "string"
    && ["success", "blocked", "timeout", "rejected", "emergency_stop"].includes(value.status)
    && isRecord(value.observation)
    && (value.error_code === null || value.error_code === undefined || typeof value.error_code === "string")
    && (value.error_message === null || value.error_message === undefined || typeof value.error_message === "string")
    && isInteger(value.received_at_ms);
}

function isToolCall(value: unknown): boolean {
  if (!isRecord(value) || !hasExactKeys(value, ["name", "arguments"])) return false;
  return isToolName(value.name) && isRecord(value.arguments);
}

function isPayload(phase: RuntimeEventPhase, value: unknown): value is Record<string, unknown> {
  if (!isRecord(value)) return false;
  if (phase === "user_goal") {
    return hasExactKeys(value, ["goal", "planner", "max_steps"])
      && isString(value.goal)
      && (value.planner === "fake" || value.planner === "model")
      && isInteger(value.max_steps, 1)
      && Number(value.max_steps) <= 16;
  }
  if (phase === "planning") {
    return hasExactKeys(value, ["step_count", "max_steps", "blocked_triggered"])
      && isInteger(value.step_count)
      && isInteger(value.max_steps, 1)
      && Number(value.max_steps) <= 16
      && typeof value.blocked_triggered === "boolean";
  }
  if (phase === "tool_call") {
    return hasExactKeys(value, ["planner_message", "arguments"])
      && isString(value.planner_message)
      && isRecord(value.arguments);
  }
  if (phase === "published") {
    return hasExactKeys(value, ["topic", "qos", "command", "published"])
      && isString(value.topic, 256)
      && value.qos === 1
      && value.published === true
      && isCommand(value.command);
  }
  if (phase === "observation") {
    return hasExactKeys(value, ["observation", "latency_ms"])
      && isObservation(value.observation)
      && isInteger(value.latency_ms);
  }
  if (phase === "replanning") {
    return hasExactKeys(value, ["reason", "last_status", "next_seq"])
      && isString(value.reason, 256)
      && typeof value.last_status === "string"
      && isInteger(value.next_seq, 1);
  }
  if (phase === "safety_rejected") {
    return hasExactKeys(value, ["tool_call", "observation", "published"])
      && isToolCall(value.tool_call)
      && isObservation(value.observation)
      && value.published === false;
  }
  return hasExactKeys(value, ["final_status", "final_message", "duration_ms", "step_count"])
    && isString(value.final_status, 64)
    && isString(value.final_message)
    && isInteger(value.duration_ms)
    && isInteger(value.step_count)
    && Number(value.step_count) <= 16;
}

export function parseRuntimeEvent(value: unknown): RuntimeEvent | null {
  const keys = ["type", "version", "event_id", "timestamp_ms", "task_id", "seq", "phase", "tool", "payload"] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys)) return null;
  if (
    value.type !== "agent.step"
    || value.version !== 1
    || !isIdentifier(value.event_id)
    || !isInteger(value.timestamp_ms)
    || !isIdentifier(value.task_id)
    || !isInteger(value.seq)
    || typeof value.phase !== "string"
    || !PHASES.has(value.phase as RuntimeEventPhase)
  ) {
    return null;
  }
  const phase = value.phase as RuntimeEventPhase;
  const taskLevel = phase === "user_goal" || phase === "final";
  if ((taskLevel && value.seq !== 0) || (!taskLevel && Number(value.seq) < 1)) return null;
  if (TOOL_PHASES.has(phase)) {
    if (!isToolName(value.tool)) return null;
  } else if (value.tool !== null) {
    return null;
  }
  if (!isPayload(phase, value.payload)) return null;
  return value as RuntimeEvent;
}

export function parseRuntimeCapabilities(value: unknown): RuntimeCapabilities | null {
  const keys = [
    "version", "mode", "device_id", "mqtt_endpoint", "fake_planner", "model_configured",
    "fault_injection",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys)) return null;
  if (
    value.version !== 1
    || (value.mode !== "preview" && value.mode !== "bridge")
    || (value.device_id !== null && !isIdentifier(value.device_id))
    || (value.mqtt_endpoint !== null && !isString(value.mqtt_endpoint, 256))
    || value.fake_planner !== true
    || typeof value.model_configured !== "boolean"
    || typeof value.fault_injection !== "boolean"
  ) {
    return null;
  }
  return value as RuntimeCapabilities;
}

export function parseMissionTaskSnapshot(value: unknown): MissionTaskSnapshot | null {
  const keys = [
    "version", "task_id", "goal", "planner", "max_steps", "status", "accepted_at_ms",
    "started_at_ms", "finished_at_ms", "final_status", "final_message", "events",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys) || !Array.isArray(value.events)) return null;
  const events = value.events.map(parseRuntimeEvent);
  if (
    value.version !== 1
    || !isIdentifier(value.task_id)
    || !isString(value.goal)
    || (value.planner !== "fake" && value.planner !== "model")
    || !isInteger(value.max_steps, 1)
    || Number(value.max_steps) > 16
    || !["accepted", "running", "finished"].includes(String(value.status))
    || !isInteger(value.accepted_at_ms)
    || (value.started_at_ms !== null && !isInteger(value.started_at_ms))
    || (value.finished_at_ms !== null && !isInteger(value.finished_at_ms))
    || (value.final_status !== null && !isString(value.final_status, 64))
    || (value.final_message !== null && !isString(value.final_message))
    || events.some((event) => event === null)
  ) {
    return null;
  }
  return { ...value, events: events as RuntimeEvent[] } as MissionTaskSnapshot;
}

export function parseRuntimeApiError(value: unknown): RuntimeApiError | null {
  if (!isRecord(value) || !hasExactKeys(value, ["version", "error_code", "error_message"])) return null;
  if (value.version !== 1 || !isString(value.error_code, 64) || !isString(value.error_message, 256)) return null;
  return value as RuntimeApiError;
}

export function mergeRuntimeEvents(
  base: readonly RuntimeEvent[],
  incoming: readonly RuntimeEvent[],
): RuntimeEvent[] {
  const merged: RuntimeEvent[] = [];
  const seen = new Set<string>();
  for (const event of [...base, ...incoming]) {
    if (seen.has(event.event_id)) continue;
    seen.add(event.event_id);
    merged.push(event);
  }
  return merged;
}
