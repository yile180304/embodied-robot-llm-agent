import assert from "node:assert/strict";
import test from "node:test";

import { parseWorldConfig } from "../src/world.ts";

function worldConfig() {
  return {
    version: 1,
    scene_id: "indoor-lab-obstacle-left",
    bounds: { min_x: -6, max_x: 6, min_y: -4, max_y: 4 },
    robot_spawn: { x_m: 0, y_m: 0, yaw_deg: 0 },
    robot_footprint_radius_m: 0.35,
    stop_clearance_m: 0.25,
    sensor_max_range_m: 10,
    obstacles: [
      {
        id: "front-crate",
        center_x_m: 1.2,
        center_y_m: 0,
        size_x_m: 0.5,
        size_y_m: 0.8,
        height_m: 0.7,
      },
    ],
    semantic_objects: [],
  };
}

function semanticObject(kind: string, index: number) {
  return {
    id: `${kind}-${index}`,
    kind,
    label: `${kind} ${index}`,
    color: kind === "bottle" ? "red" : kind === "goal_zone" ? "blue" : "gray",
    center_x_m: -4.5 + index * 1.2,
    center_y_m: 2.5,
    size_x_m: kind === "goal_zone" ? 1 : 0.4,
    size_y_m: kind === "goal_zone" ? 1 : 0.4,
    height_m: kind === "goal_zone" ? 0.03 : 0.8,
    interaction_radius_m: 0.7,
    blocking: !["bottle", "goal_zone"].includes(kind),
  };
}

test("parses strict World Config v1", () => {
  const config = worldConfig();

  assert.deepEqual(parseWorldConfig(config), config);
});

test("parses all fixed semantic object kinds", () => {
  const semanticObjects = ["person", "table", "chair", "bottle", "door", "goal_zone"]
    .map(semanticObject);
  const config = { ...worldConfig(), semantic_objects: semanticObjects };
  assert.deepEqual(parseWorldConfig(config), config);
});

test("rejects unknown fields, versions, duplicate ids, and invalid spawn geometry", () => {
  assert.equal(parseWorldConfig({ ...worldConfig(), version: 2 }), null);
  assert.equal(parseWorldConfig({ ...worldConfig(), extra: true }), null);
  assert.equal(
    parseWorldConfig({
      ...worldConfig(),
      obstacles: [worldConfig().obstacles[0], worldConfig().obstacles[0]],
    }),
    null,
  );
  assert.equal(
    parseWorldConfig({
      ...worldConfig(),
      robot_spawn: { x_m: 1.2, y_m: 0, yaw_deg: 0 },
    }),
    null,
  );
  assert.equal(
    parseWorldConfig({
      ...worldConfig(),
      semantic_objects: [{
        id: "bottle-red-01",
        kind: "bottle",
        label: "red bottle",
        color: "red",
        center_x_m: 0,
        center_y_m: 2,
        size_x_m: 0.12,
        size_y_m: 0.12,
        height_m: 0.28,
        interaction_radius_m: 0.55,
        blocking: false,
      }, {
        id: "bottle-red-01",
        kind: "bottle",
        label: "duplicate",
        color: "red",
        center_x_m: 1,
        center_y_m: 2,
        size_x_m: 0.12,
        size_y_m: 0.12,
        height_m: 0.28,
        interaction_radius_m: 0.55,
        blocking: false,
      }],
    }),
    null,
  );
  assert.equal(
    parseWorldConfig({
      ...worldConfig(),
      semantic_objects: [{
        id: "bad-kind",
        kind: "camera",
        label: "bad",
        color: "red",
        center_x_m: 0,
        center_y_m: 2,
        size_x_m: 0.12,
        size_y_m: 0.12,
        height_m: 0.28,
        interaction_radius_m: 0.55,
        blocking: false,
      }],
    }),
    null,
  );
});
