import assert from "node:assert/strict";
import test from "node:test";

import { parseFaultRunSnapshot } from "../src/fault.ts";
import { buildFaultEvidenceRows } from "../src/fault_console.ts";

const observation = {
  version: 1,
  task_id: "fault-test-task",
  seq: 1,
  status: "success",
  observation: { yaw_deg: 30 },
  error_code: null,
  error_message: null,
  received_at_ms: 1_100,
};

const snapshot = {
  version: 1,
  run_id: "fault-test",
  scenario: "duplicate_delivery",
  status: "finished",
  accepted_at_ms: 1_000,
  started_at_ms: 1_001,
  finished_at_ms: 1_200,
  result: "passed",
  summary: "same command replayed without a second turn",
  evidence: [
    {
      event_id: "fault-evt-prepare",
      timestamp_ms: 1_001,
      stage: "prepare",
      task_id: null,
      seq: null,
      payload: { scenario: "duplicate_delivery" },
    },
    {
      event_id: "fault-evt-observation",
      timestamp_ms: 1_100,
      stage: "observation",
      task_id: "fault-test-task",
      seq: 1,
      payload: { observation, replayed: true },
    },
    {
      event_id: "fault-evt-final",
      timestamp_ms: 1_200,
      stage: "final",
      task_id: null,
      seq: null,
      payload: {
        result: "passed",
        summary: "same command replayed without a second turn",
      },
    },
  ],
} as const;

test("parses strict fault snapshots and rejects unknown scenario fields", () => {
  assert.deepEqual(parseFaultRunSnapshot(snapshot), snapshot);
  assert.equal(parseFaultRunSnapshot({ ...snapshot, raw_topic: "robot/dog01/cmd" }), null);
  assert.equal(parseFaultRunSnapshot({ ...snapshot, scenario: "random_loss" }), null);
});

test("rejects mismatched evidence payloads", () => {
  const invalid = {
    ...snapshot,
    evidence: [{
      ...snapshot.evidence[0],
      payload: { scenario: "duplicate_delivery", duration_ms: 500 },
    }],
  };
  assert.equal(parseFaultRunSnapshot(invalid), null);
});

test("builds compact evidence rows without merging into the agent timeline", () => {
  const parsed = parseFaultRunSnapshot(snapshot);
  assert.ok(parsed);
  const rows = buildFaultEvidenceRows(parsed.evidence);

  assert.deepEqual(rows.map((row) => row.label), ["Prepare", "Observation / Replay", "Fault Final"]);
  assert.match(rows[1].detail, /success/);
  assert.equal(rows[2].tone, "final");
});
