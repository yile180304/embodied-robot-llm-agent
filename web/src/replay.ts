import { parseFaultRunSnapshot, type FaultEvidence, type FaultRunSnapshot } from "./fault.ts";
import { parseMissionTaskSnapshot, type MissionTaskSnapshot, type RuntimeEvent } from "./mission.ts";
import { parseSimulationFrame, type SimulationFrame } from "./simulation.ts";
import { parseWorldConfig, type WorldConfig } from "./world.ts";

export const MAX_REPLAY_FILE_BYTES = 5 * 1024 * 1024;
export const MAX_REPLAY_FRAMES = 512;

export type ReplayFrameReason =
  | "start"
  | "cadence"
  | "command_change"
  | "state_change"
  | "terminal";

export type ReplayFrameSample = {
  offset_ms: number;
  reason: ReplayFrameReason;
  frame: SimulationFrame;
};

export type ReplayFrameCapture = {
  cadence_ms: 200;
  count: number;
  truncated: boolean;
};

export type TruthfulnessProfile = {
  planner: "fake_planner" | "openai_compatible_provider" | "not_applicable";
  perception_source: "python_simulation_ground_truth";
  mqtt_scope: "localhost_mqtt" | "configured_remote_or_unknown" | "not_used";
  model_transcript: "not_verified" | "native_tool_calls_verified" | "not_applicable";
  hardware: "not_tested";
  emqx: "not_tested";
  claims: string[];
};

export type MissionReplayRun = {
  kind: "mission";
  snapshot: MissionTaskSnapshot;
};

export type FaultReplayRun = {
  kind: "fault";
  snapshot: FaultRunSnapshot;
};

export type ReplayRun = MissionReplayRun | FaultReplayRun;

export type ReplayBundle = {
  type: "simulation.replay";
  version: 1;
  replay_id: string;
  created_at_ms: number;
  run: ReplayRun;
  world: WorldConfig;
  frame_capture: ReplayFrameCapture;
  frames: ReplayFrameSample[];
  truthfulness: TruthfulnessProfile;
};

export type ReplaySpeed = 0.5 | 1 | 2;
export type ReplayPlaybackStatus = "paused" | "playing" | "ended";

export type ReplayPlaybackState = {
  status: ReplayPlaybackStatus;
  cursor_ms: number;
  duration_ms: number;
  speed: ReplaySpeed;
};

export type ReplayProjection = {
  frame: SimulationFrame;
  mission: MissionTaskSnapshot | null;
  fault: FaultRunSnapshot | null;
  visibleEvents: RuntimeEvent[];
  visibleEvidence: FaultEvidence[];
};

const FRAME_REASONS = new Set<ReplayFrameReason>([
  "start",
  "cadence",
  "command_change",
  "state_change",
  "terminal",
]);
const PLANNERS = new Set<TruthfulnessProfile["planner"]>([
  "fake_planner",
  "openai_compatible_provider",
  "not_applicable",
]);
const MQTT_SCOPES = new Set<TruthfulnessProfile["mqtt_scope"]>([
  "localhost_mqtt",
  "configured_remote_or_unknown",
  "not_used",
]);
const MODEL_TRANSCRIPTS = new Set<TruthfulnessProfile["model_transcript"]>([
  "not_verified",
  "native_tool_calls_verified",
  "not_applicable",
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

function parseReplayRun(value: unknown): ReplayRun | null {
  if (!isRecord(value) || !hasExactKeys(value, ["kind", "snapshot"])) return null;
  if (value.kind === "mission") {
    const snapshot = parseMissionTaskSnapshot(value.snapshot);
    return snapshot ? { kind: "mission", snapshot } : null;
  }
  if (value.kind === "fault") {
    const snapshot = parseFaultRunSnapshot(value.snapshot);
    return snapshot ? { kind: "fault", snapshot } : null;
  }
  return null;
}

function parseFrameSample(value: unknown): ReplayFrameSample | null {
  if (!isRecord(value) || !hasExactKeys(value, ["offset_ms", "reason", "frame"])) return null;
  if (!isInteger(value.offset_ms) || typeof value.reason !== "string") return null;
  if (!FRAME_REASONS.has(value.reason as ReplayFrameReason)) return null;
  if (
    !isRecord(value.frame)
    || !hasExactKeys(value.frame, ["type", "version", "revision", "sim_time_ms", "robot", "sensors", "active_command"])
  ) {
    return null;
  }
  const frame = parseSimulationFrame(value.frame);
  return frame ? { offset_ms: value.offset_ms, reason: value.reason as ReplayFrameReason, frame } : null;
}

function parseFrameCapture(value: unknown): ReplayFrameCapture | null {
  if (!isRecord(value) || !hasExactKeys(value, ["cadence_ms", "count", "truncated"])) return null;
  if (
    value.cadence_ms !== 200
    || !isInteger(value.count, 1)
    || Number(value.count) > MAX_REPLAY_FRAMES
    || typeof value.truncated !== "boolean"
  ) {
    return null;
  }
  return value as ReplayFrameCapture;
}

function parseTruthfulness(value: unknown): TruthfulnessProfile | null {
  const keys = [
    "planner", "perception_source", "mqtt_scope", "model_transcript", "hardware", "emqx", "claims",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys) || !Array.isArray(value.claims)) return null;
  if (
    typeof value.planner !== "string"
    || !PLANNERS.has(value.planner as TruthfulnessProfile["planner"])
    || value.perception_source !== "python_simulation_ground_truth"
    || typeof value.mqtt_scope !== "string"
    || !MQTT_SCOPES.has(value.mqtt_scope as TruthfulnessProfile["mqtt_scope"])
    || typeof value.model_transcript !== "string"
    || !MODEL_TRANSCRIPTS.has(value.model_transcript as TruthfulnessProfile["model_transcript"])
    || value.hardware !== "not_tested"
    || value.emqx !== "not_tested"
    || value.claims.length < 1
    || value.claims.length > 8
    || value.claims.some((claim) => typeof claim !== "string" || claim.length < 1 || claim.length > 256)
  ) {
    return null;
  }
  return { ...value, claims: [...value.claims] } as TruthfulnessProfile;
}

function hasVerifiedNativeToolChain(snapshot: MissionTaskSnapshot): boolean {
  const toolCalls = new Map<string, number>();
  const published = new Map<string, number>();
  const completed: number[] = [];
  for (const [index, event] of snapshot.events.entries()) {
    if (event.phase === "final") {
      if (completed.some((completedIndex) => completedIndex < index)) return true;
      continue;
    }
    if (event.task_id !== snapshot.task_id || event.tool === null) continue;
    const key = `${event.seq}:${event.tool}`;
    if (event.phase === "tool_call") {
      if (!toolCalls.has(key)) toolCalls.set(key, index);
      continue;
    }
    if (event.phase === "published") {
      const command = event.payload.command;
      const callIndex = toolCalls.get(key);
      if (
        callIndex !== undefined
        && callIndex < index
        && isRecord(command)
        && command.task_id === snapshot.task_id
        && command.seq === event.seq
        && command.tool === event.tool
        && event.payload.published === true
      ) {
        if (!published.has(key)) published.set(key, index);
      }
      continue;
    }
    if (event.phase === "observation") {
      const observation = event.payload.observation;
      const publishIndex = published.get(key);
      if (
        publishIndex !== undefined
        && publishIndex < index
        && isRecord(observation)
        && observation.task_id === snapshot.task_id
        && observation.seq === event.seq
      ) {
        completed.push(index);
      }
    }
  }
  return false;
}

export function parseReplayBundle(value: unknown): ReplayBundle | null {
  const keys = [
    "type", "version", "replay_id", "created_at_ms", "run", "world", "frame_capture", "frames", "truthfulness",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys) || !Array.isArray(value.frames)) return null;
  if (
    value.type !== "simulation.replay"
    || value.version !== 1
    || !isIdentifier(value.replay_id)
    || !isInteger(value.created_at_ms)
    || value.frames.length < 1
    || value.frames.length > MAX_REPLAY_FRAMES
  ) {
    return null;
  }
  const run = parseReplayRun(value.run);
  const world = parseWorldConfig(value.world);
  const frameCapture = parseFrameCapture(value.frame_capture);
  const truthfulness = parseTruthfulness(value.truthfulness);
  const frames = value.frames.map(parseFrameSample);
  if (!run || !world || !frameCapture || !truthfulness || frames.some((frame) => frame === null)) return null;
  const typedFrames = frames as ReplayFrameSample[];
  if (frameCapture.count !== typedFrames.length) return null;
  if (typedFrames[0].reason !== "start" || typedFrames.at(-1)?.reason !== "terminal") return null;
  for (let index = 1; index < typedFrames.length; index += 1) {
    if (typedFrames[index].offset_ms < typedFrames[index - 1].offset_ms) return null;
  }
  const expectedPlanner = run.kind === "fault"
    ? "not_applicable"
    : run.snapshot.planner === "fake" ? "fake_planner" : "openai_compatible_provider";
  const expectedTranscript = run.kind === "fault"
    ? "not_applicable"
    : run.snapshot.planner === "model" && hasVerifiedNativeToolChain(run.snapshot)
      ? "native_tool_calls_verified"
      : "not_verified";
  if (
    truthfulness.planner !== expectedPlanner
    || truthfulness.model_transcript !== expectedTranscript
  ) return null;
  return {
    type: "simulation.replay",
    version: 1,
    replay_id: value.replay_id,
    created_at_ms: value.created_at_ms,
    run,
    world,
    frame_capture: frameCapture,
    frames: typedFrames,
    truthfulness,
  };
}

export function parseReplayFileText(text: string): ReplayBundle | null {
  if (new TextEncoder().encode(text).byteLength > MAX_REPLAY_FILE_BYTES) return null;
  try {
    return parseReplayBundle(JSON.parse(text) as unknown);
  } catch {
    return null;
  }
}

function eventOffset(timestampMs: number, originMs: number): number {
  return Math.max(0, timestampMs - originMs);
}

export function replayDuration(bundle: ReplayBundle): number {
  const frameDuration = bundle.frames.at(-1)?.offset_ms ?? 0;
  const origin = bundle.run.snapshot.accepted_at_ms;
  const eventDuration = bundle.run.kind === "mission"
    ? bundle.run.snapshot.events.reduce(
      (maximum: number, item: RuntimeEvent) => Math.max(maximum, eventOffset(item.timestamp_ms, origin)),
      0,
    )
    : bundle.run.snapshot.evidence.reduce(
      (maximum: number, item: FaultEvidence) => Math.max(maximum, eventOffset(item.timestamp_ms, origin)),
      0,
    );
  return Math.max(frameDuration, eventDuration);
}

export function initialReplayPlayback(bundle: ReplayBundle): ReplayPlaybackState {
  return { status: "paused", cursor_ms: 0, duration_ms: replayDuration(bundle), speed: 1 };
}

export function setReplayPlaying(state: ReplayPlaybackState, playing: boolean): ReplayPlaybackState {
  if (!playing) return { ...state, status: "paused" };
  if (state.cursor_ms >= state.duration_ms) return { ...state, cursor_ms: 0, status: "playing" };
  return { ...state, status: "playing" };
}

export function setReplaySpeed(state: ReplayPlaybackState, speed: ReplaySpeed): ReplayPlaybackState {
  return { ...state, speed };
}

export function seekReplay(state: ReplayPlaybackState, cursorMs: number): ReplayPlaybackState {
  const cursor = Math.max(0, Math.min(state.duration_ms, cursorMs));
  return {
    ...state,
    cursor_ms: cursor,
    status: cursor >= state.duration_ms ? "ended" : "paused",
  };
}

export function advanceReplay(state: ReplayPlaybackState, elapsedMs: number): ReplayPlaybackState {
  if (state.status !== "playing" || !Number.isFinite(elapsedMs) || elapsedMs <= 0) return state;
  const cursor = Math.min(state.duration_ms, state.cursor_ms + elapsedMs * state.speed);
  return { ...state, cursor_ms: cursor, status: cursor >= state.duration_ms ? "ended" : "playing" };
}

export function stepReplay(
  bundle: ReplayBundle,
  state: ReplayPlaybackState,
  direction: -1 | 1,
): ReplayPlaybackState {
  const offsets = [...new Set(bundle.frames.map((sample) => sample.offset_ms))];
  if (direction > 0) {
    const next = offsets.find((offset) => offset > state.cursor_ms) ?? state.duration_ms;
    return seekReplay(state, next);
  }
  const previous = offsets.filter((offset) => offset < state.cursor_ms).at(-1) ?? 0;
  return seekReplay(state, previous);
}

export function projectReplay(bundle: ReplayBundle, state: ReplayPlaybackState): ReplayProjection {
  let selected = bundle.frames[0];
  for (const sample of bundle.frames) {
    if (sample.offset_ms > state.cursor_ms) break;
    selected = sample;
  }
  const origin = bundle.run.snapshot.accepted_at_ms;
  if (bundle.run.kind === "mission") {
    const visibleEvents = bundle.run.snapshot.events.filter(
      (event) => eventOffset(event.timestamp_ms, origin) <= state.cursor_ms,
    );
    return {
      frame: selected.frame,
      mission: { ...bundle.run.snapshot, events: visibleEvents },
      fault: null,
      visibleEvents,
      visibleEvidence: [],
    };
  }
  const visibleEvidence = bundle.run.snapshot.evidence.filter(
    (item) => eventOffset(item.timestamp_ms, origin) <= state.cursor_ms,
  );
  return {
    frame: selected.frame,
    mission: null,
    fault: { ...bundle.run.snapshot, evidence: visibleEvidence },
    visibleEvents: [],
    visibleEvidence,
  };
}
