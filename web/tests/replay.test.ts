import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_REPLAY_FILE_BYTES,
  advanceReplay,
  initialReplayPlayback,
  parseReplayBundle,
  parseReplayFileText,
  projectReplay,
  seekReplay,
  setReplayPlaying,
  setReplaySpeed,
  stepReplay,
  type ReplayBundle,
} from "../src/replay.ts";
import { formatReplayTime, modelTranscriptLabel, replayEvidenceCount } from "../src/replay_console.ts";

function frame(revision: number, x = 0) {
  return {
    type: "simulation.frame",
    version: 1,
    revision,
    sim_time_ms: revision * 50,
    robot: {
      x_m: x,
      y_m: 0,
      yaw_deg: 0,
      linear_speed_mps: 0,
      angular_speed_dps: 0,
      gait: "stand",
      gait_phase: 0,
      emergency_stopped: false,
    },
    sensors: {
      front_distance_cm: 1_000,
      left_distance_cm: 1_000,
      right_distance_cm: 1_000,
    },
    active_command: null,
  } as const;
}

function bundle(): ReplayBundle {
  return {
    type: "simulation.replay",
    version: 1,
    replay_id: "replay-test",
    created_at_ms: 1_500,
    run: {
      kind: "mission",
      snapshot: {
        version: 1,
        task_id: "mission-test",
        goal: "前进 1 米",
        planner: "fake",
        max_steps: 8,
        status: "finished",
        accepted_at_ms: 1_000,
        started_at_ms: 1_001,
        finished_at_ms: 1_500,
        final_status: "success",
        final_message: "done",
        events: [
          {
            type: "agent.step",
            version: 1,
            event_id: "evt-goal",
            timestamp_ms: 1_000,
            task_id: "mission-test",
            seq: 0,
            phase: "user_goal",
            tool: null,
            payload: { goal: "前进 1 米", planner: "fake", max_steps: 8 },
          },
          {
            type: "agent.step",
            version: 1,
            event_id: "evt-final",
            timestamp_ms: 1_500,
            task_id: "mission-test",
            seq: 0,
            phase: "final",
            tool: null,
            payload: { final_status: "success", final_message: "done", duration_ms: 500, step_count: 1 },
          },
        ],
      },
    },
    world: {
      version: 1,
      scene_id: "replay-world",
      bounds: { min_x: -5, max_x: 5, min_y: -5, max_y: 5 },
      robot_spawn: { x_m: 0, y_m: 0, yaw_deg: 0 },
      robot_footprint_radius_m: 0.35,
      stop_clearance_m: 0.25,
      sensor_max_range_m: 10,
      obstacles: [],
      semantic_objects: [],
    },
    frame_capture: { cadence_ms: 200, count: 3, truncated: false },
    frames: [
      { offset_ms: 0, reason: "start", frame: frame(1) },
      { offset_ms: 250, reason: "cadence", frame: frame(2, 0.5) },
      { offset_ms: 500, reason: "terminal", frame: frame(3, 1) },
    ],
    truthfulness: {
      planner: "fake_planner",
      perception_source: "python_simulation_ground_truth",
      mqtt_scope: "localhost_mqtt",
      model_transcript: "not_verified",
      hardware: "not_tested",
      emqx: "not_tested",
      claims: ["Python simulation only"],
    },
  };
}

test("parses a strict replay bundle and rejects unknown or inconsistent fields", () => {
  const value = bundle();
  assert.deepEqual(parseReplayBundle(value), value);
  assert.equal(parseReplayBundle({ ...value, version: 2 }), null);
  assert.equal(parseReplayBundle({ ...value, api_key: "secret" }), null);
  assert.equal(parseReplayBundle({ ...value, frame_capture: { ...value.frame_capture, count: 2 } }), null);
  assert.equal(parseReplayBundle({ ...value, frames: [value.frames[1], value.frames[0], value.frames[2]] }), null);
  assert.equal(parseReplayBundle({ ...value, frames: value.frames.map((item) => ({ ...item, frame: { ...item.frame, extra: true } })) }), null);
  assert.equal(parseReplayBundle({
    ...value,
    truthfulness: { ...value.truthfulness, model_transcript: "native_tool_calls_verified" },
  }), null);
  assert.equal(parseReplayBundle({
    ...value,
    truthfulness: { ...value.truthfulness, model_transcript: "configured_only" },
  }), null);
});

test("parses local JSON within the size limit and rejects oversized text", () => {
  assert.deepEqual(parseReplayFileText(JSON.stringify(bundle())), bundle());
  assert.equal(parseReplayFileText("{"), null);
  assert.equal(parseReplayFileText("x".repeat(MAX_REPLAY_FILE_BYTES + 1)), null);
});

test("projects frames and mission events at the playback cursor", () => {
  const value = bundle();
  const initial = initialReplayPlayback(value);
  assert.equal(projectReplay(value, initial).frame.revision, 1);
  assert.equal(projectReplay(value, initial).visibleEvents.length, 1);

  const middle = seekReplay(initial, 300);
  assert.equal(projectReplay(value, middle).frame.revision, 2);
  assert.equal(projectReplay(value, middle).visibleEvents.length, 1);

  const ended = seekReplay(initial, 500);
  assert.equal(projectReplay(value, ended).frame.revision, 3);
  assert.equal(projectReplay(value, ended).visibleEvents.length, 2);
  assert.equal(ended.status, "ended");
});

test("plays, changes speed, advances, and steps only on recorded offsets", () => {
  const value = bundle();
  let state = setReplayPlaying(initialReplayPlayback(value), true);
  state = setReplaySpeed(state, 2);
  state = advanceReplay(state, 125);
  assert.equal(state.cursor_ms, 250);
  assert.equal(state.status, "playing");

  state = stepReplay(value, state, 1);
  assert.equal(state.cursor_ms, 500);
  assert.equal(state.status, "ended");
  state = stepReplay(value, state, -1);
  assert.equal(state.cursor_ms, 250);
  assert.equal(state.status, "paused");
});

test("formats replay workbench time and evidence counts", () => {
  assert.equal(formatReplayTime(0), "00:00.0");
  assert.equal(formatReplayTime(65_432), "01:05.4");
  assert.equal(replayEvidenceCount(bundle()), 2);
  assert.equal(modelTranscriptLabel("native_tool_calls_verified"), "Verified native Tool Calls");
  assert.equal(modelTranscriptLabel("not_verified"), "Not verified");
});
