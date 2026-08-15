import assert from "node:assert/strict";
import test from "node:test";

import {
  acceptSimulationFrame,
  initialSimulationViewState,
  parseSimulationFrame,
  toThreePose,
  type SimulationFrame,
} from "../src/simulation.ts";

function frame(revision: number, overrides: Partial<SimulationFrame["robot"]> = {}): SimulationFrame {
  return {
    type: "simulation.frame",
    version: 1,
    revision,
    sim_time_ms: revision * 50,
    robot: {
      x_m: 0,
      y_m: 0,
      yaw_deg: 0,
      linear_speed_mps: 0.5,
      angular_speed_dps: 0,
      gait: "walk",
      gait_phase: 0.25,
      emergency_stopped: false,
      ...overrides,
    },
    sensors: {
      front_distance_cm: 1_000,
      left_distance_cm: 1_000,
      right_distance_cm: 1_000,
    },
    active_command: {
      task_id: "demo-program",
      seq: 1,
      tool: "move_robot",
      progress: 0.5,
    },
  };
}

test("accepts only newer revisions", () => {
  const first = acceptSimulationFrame(initialSimulationViewState, frame(10));
  const duplicate = acceptSimulationFrame(first, frame(10, { x_m: 99 }));
  const stale = acceptSimulationFrame(first, frame(9, { x_m: 99 }));
  const newer = acceptSimulationFrame(first, frame(11, { x_m: 1 }));

  assert.equal(duplicate, first);
  assert.equal(stale, first);
  assert.equal(newer.previousFrame, first.latestFrame);
  assert.equal(newer.latestFrame?.robot.x_m, 1);
});

test("maps simulation axes and positive yaw to Three.js", () => {
  const pose = toThreePose(frame(1, { x_m: 2, y_m: 1, yaw_deg: 90 }));

  assert.equal(pose.x, 2);
  assert.equal(pose.z, 1);
  assert.equal(pose.yawRadians, -Math.PI / 2);
});

test("rejects malformed WebSocket payloads", () => {
  assert.equal(parseSimulationFrame({ type: "simulation.frame", version: 1 }), null);
  assert.equal(parseSimulationFrame({ ...frame(1), revision: -1 }), null);
  assert.deepEqual(parseSimulationFrame(frame(1)), frame(1));
});

test("accepts real Agent task ids in active commands", () => {
  const agentFrame = frame(2);
  agentFrame.active_command = {
    task_id: "simulation-agent:42",
    seq: 3,
    tool: "turn_robot",
    progress: 0.4,
  };

  assert.deepEqual(parseSimulationFrame(agentFrame), agentFrame);
  assert.equal(
    parseSimulationFrame({
      ...agentFrame,
      active_command: { ...agentFrame.active_command, task_id: "bad task id" },
    }),
    null,
  );
});
