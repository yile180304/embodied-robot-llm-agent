export type FaultScenario =
  | "device_disconnect"
  | "response_timeout"
  | "duplicate_delivery"
  | "out_of_order";

export type FaultEvidenceStage =
  | "prepare"
  | "published"
  | "observation"
  | "timeout"
  | "service_disconnected"
  | "service_restored"
  | "final";

export type FaultEvidence = {
  event_id: string;
  timestamp_ms: number;
  stage: FaultEvidenceStage;
  task_id: string | null;
  seq: number | null;
  payload: Record<string, unknown>;
};

export type FaultRunSnapshot = {
  version: 1;
  run_id: string;
  scenario: FaultScenario;
  status: "accepted" | "running" | "finished";
  accepted_at_ms: number;
  started_at_ms: number | null;
  finished_at_ms: number | null;
  result: "passed" | "failed" | null;
  summary: string | null;
  evidence: FaultEvidence[];
};

const SCENARIOS = new Set<FaultScenario>([
  "device_disconnect",
  "response_timeout",
  "duplicate_delivery",
  "out_of_order",
]);
const STAGES = new Set<FaultEvidenceStage>([
  "prepare",
  "published",
  "observation",
  "timeout",
  "service_disconnected",
  "service_restored",
  "final",
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

function isString(value: unknown, maxLength = 512): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength;
}

function isScenario(value: unknown): value is FaultScenario {
  return typeof value === "string" && SCENARIOS.has(value as FaultScenario);
}

function isCommand(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return value.version === 1
    && isIdentifier(value.task_id)
    && isInteger(value.seq, 1)
    && typeof value.tool === "string"
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
    && (value.error_code === null || typeof value.error_code === "string")
    && (value.error_message === null || typeof value.error_message === "string")
    && isInteger(value.received_at_ms);
}

function isEvidencePayload(stage: FaultEvidenceStage, value: unknown): value is Record<string, unknown> {
  if (!isRecord(value)) return false;
  if (stage === "prepare") {
    return hasExactKeys(value, ["scenario"]) && isScenario(value.scenario);
  }
  if (stage === "published") {
    return hasExactKeys(value, ["topic", "qos", "command", "published"])
      && isString(value.topic, 256)
      && value.qos === 1
      && isCommand(value.command)
      && value.published === true;
  }
  if (stage === "observation") {
    return hasExactKeys(value, ["observation", "replayed"])
      && isObservation(value.observation)
      && typeof value.replayed === "boolean";
  }
  if (stage === "timeout") {
    return hasExactKeys(value, ["timeout_ms", "error_message"])
      && isInteger(value.timeout_ms, 1)
      && isString(value.error_message, 256);
  }
  if (stage === "service_disconnected") {
    return hasExactKeys(value, ["device_id", "connected", "last_will"])
      && isIdentifier(value.device_id)
      && value.connected === false
      && value.last_will === false;
  }
  if (stage === "service_restored") {
    return hasExactKeys(value, ["device_id", "connected"])
      && isIdentifier(value.device_id)
      && value.connected === true;
  }
  return hasExactKeys(value, ["result", "summary"])
    && (value.result === "passed" || value.result === "failed")
    && isString(value.summary);
}

export function parseFaultEvidence(value: unknown): FaultEvidence | null {
  const keys = ["event_id", "timestamp_ms", "stage", "task_id", "seq", "payload"] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys)) return null;
  if (
    !isIdentifier(value.event_id)
    || !isInteger(value.timestamp_ms)
    || typeof value.stage !== "string"
    || !STAGES.has(value.stage as FaultEvidenceStage)
  ) {
    return null;
  }
  const stage = value.stage as FaultEvidenceStage;
  const commandStage = stage === "published" || stage === "observation" || stage === "timeout";
  if (commandStage) {
    if (!isIdentifier(value.task_id) || !isInteger(value.seq, 1)) return null;
  } else if (value.task_id !== null || value.seq !== null) {
    return null;
  }
  if (!isEvidencePayload(stage, value.payload)) return null;
  return value as FaultEvidence;
}

export function parseFaultRunSnapshot(value: unknown): FaultRunSnapshot | null {
  const keys = [
    "version", "run_id", "scenario", "status", "accepted_at_ms", "started_at_ms",
    "finished_at_ms", "result", "summary", "evidence",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys) || !Array.isArray(value.evidence)) return null;
  const evidence = value.evidence.map(parseFaultEvidence);
  if (
    value.version !== 1
    || !isIdentifier(value.run_id)
    || !isScenario(value.scenario)
    || !["accepted", "running", "finished"].includes(String(value.status))
    || !isInteger(value.accepted_at_ms)
    || (value.started_at_ms !== null && !isInteger(value.started_at_ms))
    || (value.finished_at_ms !== null && !isInteger(value.finished_at_ms))
    || (value.result !== null && value.result !== "passed" && value.result !== "failed")
    || (value.summary !== null && !isString(value.summary))
    || evidence.length > 64
    || evidence.some((item) => item === null)
  ) {
    return null;
  }
  return { ...value, evidence: evidence as FaultEvidence[] } as FaultRunSnapshot;
}
