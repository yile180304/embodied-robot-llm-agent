import assert from "node:assert/strict";
import test from "node:test";

import { createWorldVisuals } from "../src/world_renderer.ts";
import type { WorldConfig } from "../src/world.ts";

const config: WorldConfig = {
  version: 1,
  scene_id: "semantic-render-test",
  bounds: { min_x: -6, max_x: 6, min_y: -4, max_y: 4 },
  robot_spawn: { x_m: 0, y_m: 0, yaw_deg: 0 },
  robot_footprint_radius_m: 0.35,
  stop_clearance_m: 0.25,
  sensor_max_range_m: 10,
  obstacles: [],
  semantic_objects: [
    ["person", "green"],
    ["table", "gray"],
    ["chair", "yellow"],
    ["bottle", "red"],
    ["door", "white"],
    ["goal_zone", "blue"],
  ].map(([kind, color], index) => ({
    id: `${kind}-${index}`,
    kind: kind as WorldConfig["semantic_objects"][number]["kind"],
    label: `${kind} ${index}`,
    color: color as WorldConfig["semantic_objects"][number]["color"],
    center_x_m: -4 + index * 1.4,
    center_y_m: 2.5,
    size_x_m: kind === "goal_zone" ? 1 : 0.5,
    size_y_m: kind === "goal_zone" ? 1 : 0.5,
    height_m: kind === "goal_zone" ? 0.03 : 0.8,
    interaction_radius_m: 0.7,
    blocking: !["bottle", "goal_zone"].includes(kind),
  })),
};

test("renders one simulation-ground-truth group for every semantic object", () => {
  const visuals = createWorldVisuals(config);
  const semanticGroups = visuals.root.children.filter(
    (child) => child.userData.semanticSource === "simulation_ground_truth",
  );

  assert.equal(semanticGroups.length, 6);
  assert.deepEqual(
    semanticGroups.map((child) => child.userData.semanticKind),
    ["person", "table", "chair", "bottle", "door", "goal_zone"],
  );
});
