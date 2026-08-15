export type WorldBounds = {
  min_x: number;
  max_x: number;
  min_y: number;
  max_y: number;
};

export type RobotSpawn = {
  x_m: number;
  y_m: number;
  yaw_deg: number;
};

export type WorldObstacle = {
  id: string;
  center_x_m: number;
  center_y_m: number;
  size_x_m: number;
  size_y_m: number;
  height_m: number;
};

export type SemanticObjectKind = "person" | "table" | "chair" | "bottle" | "door" | "goal_zone";
export type SemanticObjectColor = "red" | "blue" | "green" | "yellow" | "gray" | "white";

export type WorldSemanticObject = {
  id: string;
  kind: SemanticObjectKind;
  label: string;
  color: SemanticObjectColor;
  center_x_m: number;
  center_y_m: number;
  size_x_m: number;
  size_y_m: number;
  height_m: number;
  interaction_radius_m: number;
  blocking: boolean;
};

export type WorldConfig = {
  version: 1;
  scene_id: string;
  bounds: WorldBounds;
  robot_spawn: RobotSpawn;
  robot_footprint_radius_m: number;
  stop_clearance_m: number;
  sensor_max_range_m: number;
  obstacles: WorldObstacle[];
  semantic_objects: WorldSemanticObject[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const expected = new Set(keys);
  return Object.keys(value).length === keys.length && Object.keys(value).every((key) => expected.has(key));
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isIdentifier(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/.test(value);
}

function parseBounds(value: unknown): WorldBounds | null {
  if (!isRecord(value) || !hasExactKeys(value, ["min_x", "max_x", "min_y", "max_y"])) return null;
  const minX = value.min_x;
  const maxX = value.max_x;
  const minY = value.min_y;
  const maxY = value.max_y;
  if (!isFiniteNumber(minX) || !isFiniteNumber(maxX) || !isFiniteNumber(minY) || !isFiniteNumber(maxY)) {
    return null;
  }
  if (minX >= maxX || minY >= maxY) return null;
  return {
    min_x: minX,
    max_x: maxX,
    min_y: minY,
    max_y: maxY,
  };
}

function parseSpawn(value: unknown): RobotSpawn | null {
  if (!isRecord(value) || !hasExactKeys(value, ["x_m", "y_m", "yaw_deg"])) return null;
  const x = value.x_m;
  const y = value.y_m;
  const yaw = value.yaw_deg;
  if (!isFiniteNumber(x) || !isFiniteNumber(y) || !isFiniteNumber(yaw)) return null;
  return { x_m: x, y_m: y, yaw_deg: yaw };
}

function parseObstacle(value: unknown): WorldObstacle | null {
  const keys = ["id", "center_x_m", "center_y_m", "size_x_m", "size_y_m", "height_m"] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys)) return null;
  if (!isIdentifier(value.id)) return null;
  const centerX = value.center_x_m;
  const centerY = value.center_y_m;
  const sizeX = value.size_x_m;
  const sizeY = value.size_y_m;
  const height = value.height_m;
  if (
    !isFiniteNumber(centerX)
    || !isFiniteNumber(centerY)
    || !isFiniteNumber(sizeX)
    || !isFiniteNumber(sizeY)
    || !isFiniteNumber(height)
  ) {
    return null;
  }
  if (sizeX <= 0 || sizeY <= 0 || height <= 0) return null;
  return {
    id: value.id,
    center_x_m: centerX,
    center_y_m: centerY,
    size_x_m: sizeX,
    size_y_m: sizeY,
    height_m: height,
  };
}

const semanticKinds = new Set<SemanticObjectKind>([
  "person",
  "table",
  "chair",
  "bottle",
  "door",
  "goal_zone",
]);
const semanticColors = new Set<SemanticObjectColor>(["red", "blue", "green", "yellow", "gray", "white"]);

function parseSemanticObject(value: unknown): WorldSemanticObject | null {
  const keys = [
    "id",
    "kind",
    "label",
    "color",
    "center_x_m",
    "center_y_m",
    "size_x_m",
    "size_y_m",
    "height_m",
    "interaction_radius_m",
    "blocking",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys) || !isIdentifier(value.id)) return null;
  if (typeof value.kind !== "string" || !semanticKinds.has(value.kind as SemanticObjectKind)) return null;
  if (typeof value.color !== "string" || !semanticColors.has(value.color as SemanticObjectColor)) return null;
  if (typeof value.label !== "string" || value.label.trim().length === 0 || value.label.length > 64) return null;
  if (typeof value.blocking !== "boolean") return null;
  const centerX = value.center_x_m;
  const centerY = value.center_y_m;
  const sizeX = value.size_x_m;
  const sizeY = value.size_y_m;
  const height = value.height_m;
  const interactionRadius = value.interaction_radius_m;
  if (
    !isFiniteNumber(centerX)
    || !isFiniteNumber(centerY)
    || !isFiniteNumber(sizeX)
    || !isFiniteNumber(sizeY)
    || !isFiniteNumber(height)
    || !isFiniteNumber(interactionRadius)
  ) {
    return null;
  }
  if (sizeX <= 0 || sizeY <= 0 || height <= 0 || interactionRadius <= 0 || interactionRadius > 2) return null;
  return {
    id: value.id,
    kind: value.kind as SemanticObjectKind,
    label: value.label.trim(),
    color: value.color as SemanticObjectColor,
    center_x_m: centerX,
    center_y_m: centerY,
    size_x_m: sizeX,
    size_y_m: sizeY,
    height_m: height,
    interaction_radius_m: interactionRadius,
    blocking: value.blocking,
  };
}

function pointToRectDistance(x: number, y: number, obstacle: WorldObstacle): number {
  const halfX = obstacle.size_x_m / 2;
  const halfY = obstacle.size_y_m / 2;
  const dx = Math.max(Math.abs(x - obstacle.center_x_m) - halfX, 0);
  const dy = Math.max(Math.abs(y - obstacle.center_y_m) - halfY, 0);
  return Math.hypot(dx, dy);
}

export function parseWorldConfig(value: unknown): WorldConfig | null {
  const keys = [
    "version",
    "scene_id",
    "bounds",
    "robot_spawn",
    "robot_footprint_radius_m",
    "stop_clearance_m",
    "sensor_max_range_m",
    "obstacles",
    "semantic_objects",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys) || value.version !== 1 || !isIdentifier(value.scene_id)) {
    return null;
  }
  const bounds = parseBounds(value.bounds);
  const robotSpawn = parseSpawn(value.robot_spawn);
  if (!bounds || !robotSpawn) return null;
  if (!isFiniteNumber(value.robot_footprint_radius_m) || value.robot_footprint_radius_m <= 0) return null;
  if (!isFiniteNumber(value.stop_clearance_m) || value.stop_clearance_m < 0) return null;
  if (!isFiniteNumber(value.sensor_max_range_m) || value.sensor_max_range_m <= 0) return null;
  if (!Array.isArray(value.obstacles) || !Array.isArray(value.semantic_objects)) return null;

  const obstacles: WorldObstacle[] = [];
  const ids = new Set<string>();
  for (const rawObstacle of value.obstacles) {
    const obstacle = parseObstacle(rawObstacle);
    if (!obstacle || ids.has(obstacle.id)) return null;
    ids.add(obstacle.id);
    if (
      obstacle.center_x_m - obstacle.size_x_m / 2 < bounds.min_x
      || obstacle.center_x_m + obstacle.size_x_m / 2 > bounds.max_x
      || obstacle.center_y_m - obstacle.size_y_m / 2 < bounds.min_y
      || obstacle.center_y_m + obstacle.size_y_m / 2 > bounds.max_y
    ) {
      return null;
    }
    obstacles.push(obstacle);
  }

  const semanticObjects: WorldSemanticObject[] = [];
  for (const rawItem of value.semantic_objects) {
    const item = parseSemanticObject(rawItem);
    if (!item || ids.has(item.id)) return null;
    ids.add(item.id);
    if (
      item.center_x_m - item.size_x_m / 2 < bounds.min_x
      || item.center_x_m + item.size_x_m / 2 > bounds.max_x
      || item.center_y_m - item.size_y_m / 2 < bounds.min_y
      || item.center_y_m + item.size_y_m / 2 > bounds.max_y
    ) {
      return null;
    }
    semanticObjects.push(item);
  }

  const margin = value.robot_footprint_radius_m + value.stop_clearance_m;
  if (
    bounds.max_x - bounds.min_x <= margin * 2
    || bounds.max_y - bounds.min_y <= margin * 2
    || robotSpawn.x_m - margin < bounds.min_x
    || robotSpawn.x_m + margin > bounds.max_x
    || robotSpawn.y_m - margin < bounds.min_y
    || robotSpawn.y_m + margin > bounds.max_y
    || obstacles.some((obstacle) => pointToRectDistance(robotSpawn.x_m, robotSpawn.y_m, obstacle) <= margin)
    || semanticObjects.some(
      (item) => item.blocking && pointToRectDistance(robotSpawn.x_m, robotSpawn.y_m, item) <= margin,
    )
  ) {
    return null;
  }
  return {
    version: 1,
    scene_id: value.scene_id,
    bounds,
    robot_spawn: robotSpawn,
    robot_footprint_radius_m: value.robot_footprint_radius_m,
    stop_clearance_m: value.stop_clearance_m,
    sensor_max_range_m: value.sensor_max_range_m,
    obstacles,
    semantic_objects: semanticObjects,
  };
}
