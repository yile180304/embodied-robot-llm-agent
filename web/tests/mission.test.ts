import assert from "node:assert/strict";
import test from "node:test";

import {
  mergeRuntimeEvents,
  parseMissionTaskSnapshot,
  parseRuntimeCapabilities,
  parseRuntimeEvent,
  type RuntimeEvent,
} from "../src/mission.ts";
import { buildTimelineRows, modelProviderStatus } from "../src/mission_console.ts";

function event(
  eventId: string,
  phase: RuntimeEvent["phase"],
  payload: Record<string, unknown>,
  options: { seq?: number; tool?: RuntimeEvent["tool"] } = {},
): RuntimeEvent {
  return {
    type: "agent.step",
    version: 1,
    event_id: eventId,
    timestamp_ms: 1_000,
    task_id: "mission-test",
    seq: options.seq ?? (phase === "user_goal" || phase === "final" ? 0 : 1),
    phase,
    tool: options.tool ?? null,
    payload,
  };
}

const toolCall = event(
  "evt-tool",
  "tool_call",
  { planner_message: "读取状态", arguments: {} },
  { seq: 1, tool: "get_robot_state" },
);
const published = event(
  "evt-published",
  "published",
  {
    topic: "robot/dog01/cmd",
    qos: 1,
    command: {
      version: 1,
      task_id: "mission-test",
      seq: 1,
      tool: "get_robot_state",
      params: {},
      deadline_ms: 15_000,
      sent_at_ms: 1_000,
    },
    published: true,
  },
  { seq: 1, tool: "get_robot_state" },
);

test("parses strict runtime capabilities and rejects unknown fields", () => {
  const capabilities = {
    version: 1,
    mode: "bridge",
    device_id: "dog01",
    mqtt_endpoint: "127.0.0.1:1883",
    fake_planner: true,
    model_configured: false,
    fault_injection: true,
  };

  assert.deepEqual(parseRuntimeCapabilities(capabilities), capabilities);
  assert.equal(parseRuntimeCapabilities({ ...capabilities, api_key: "secret" }), null);
  assert.match(modelProviderStatus(false), /unavailable/);
  assert.match(modelProviderStatus(true), /OpenAI-compatible/);
});

test("parses runtime events and enforces task/tool phase discipline", () => {
  assert.deepEqual(parseRuntimeEvent(toolCall), toolCall);
  assert.deepEqual(parseRuntimeEvent(published), published);
  assert.equal(parseRuntimeEvent({ ...toolCall, tool: null }), null);
  assert.equal(parseRuntimeEvent({ ...toolCall, seq: 0 }), null);
  assert.equal(parseRuntimeEvent({ ...toolCall, payload: { planner_message: "missing args" } }), null);
  const semanticCall = event(
    "evt-semantic",
    "tool_call",
    { planner_message: "查询红色瓶子", arguments: { kind: "bottle", color: "red", max_results: 4 } },
    { seq: 2, tool: "inspect_semantic_world" },
  );
  assert.deepEqual(parseRuntimeEvent(semanticCall), semanticCall);
});

test("merges current journal and live events by event id without duplication", () => {
  const goal = event("evt-goal", "user_goal", {
    goal: "读取状态",
    planner: "fake",
    max_steps: 8,
  });
  const merged = mergeRuntimeEvents([goal, toolCall], [toolCall, published]);

  assert.deepEqual(merged.map((item) => item.event_id), ["evt-goal", "evt-tool", "evt-published"]);
});

test("groups a matching tool call and MQTT publish into one action row", () => {
  const rows = buildTimelineRows([toolCall, published]);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].tone, "action");
  assert.equal(rows[0].label, "Action / MQTT Publish");
  assert.match(rows[0].detail, /MQTT QoS 1/);
});

test("parses a current task snapshot with its ordered journal", () => {
  const goal = event("evt-goal", "user_goal", {
    goal: "读取状态",
    planner: "fake",
    max_steps: 8,
  });
  const snapshot = {
    version: 1,
    task_id: "mission-test",
    goal: "读取状态",
    planner: "fake",
    max_steps: 8,
    status: "running",
    accepted_at_ms: 1_000,
    started_at_ms: 1_001,
    finished_at_ms: null,
    final_status: null,
    final_message: null,
    events: [goal, toolCall],
  };

  assert.deepEqual(parseMissionTaskSnapshot(snapshot), snapshot);
  assert.equal(parseMissionTaskSnapshot({ ...snapshot, events: [{ ...toolCall, phase: "unknown" }] }), null);
  assert.deepEqual(parseMissionTaskSnapshot({ ...snapshot, max_steps: 16 }), { ...snapshot, max_steps: 16 });
  assert.equal(parseMissionTaskSnapshot({ ...snapshot, max_steps: 17 }), null);
});
