import * as THREE from "three";
import type { SimulationFrame } from "./simulation";
import type { WorldConfig, WorldSemanticObject } from "./world";

type SensorRay = {
  line: THREE.Line<THREE.BufferGeometry, THREE.LineBasicMaterial>;
  geometry: THREE.BufferGeometry;
};

export type WorldVisuals = {
  root: THREE.Group;
  update(frame: SimulationFrame): void;
};

function makeRay(color: number): SensorRay {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(new Float32Array(6), 3));
  const line = new THREE.Line(
    geometry,
    new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.88 }),
  );
  return { line, geometry };
}

function setRay(ray: SensorRay, frame: SimulationFrame, angleDeg: number, distanceCm: number): void {
  const angle = angleDeg * Math.PI / 180;
  const startX = frame.robot.x_m;
  const startZ = frame.robot.y_m;
  const endX = startX + distanceCm / 100 * Math.cos(angle);
  const endZ = startZ + distanceCm / 100 * Math.sin(angle);
  const positions = ray.geometry.getAttribute("position") as THREE.BufferAttribute;
  positions.setXYZ(0, startX, 0.12, startZ);
  positions.setXYZ(1, endX, 0.12, endZ);
  positions.needsUpdate = true;
  ray.geometry.computeBoundingSphere();
}

function makeGrid(config: WorldConfig): THREE.LineSegments {
  const positions: number[] = [];
  for (let x = Math.ceil(config.bounds.min_x); x <= config.bounds.max_x; x += 1) {
    positions.push(x, 0.012, config.bounds.min_y, x, 0.012, config.bounds.max_y);
  }
  for (let y = Math.ceil(config.bounds.min_y); y <= config.bounds.max_y; y += 1) {
    positions.push(config.bounds.min_x, 0.012, y, config.bounds.max_x, 0.012, y);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  return new THREE.LineSegments(
    geometry,
    new THREE.LineBasicMaterial({ color: 0x29414d, transparent: true, opacity: 0.8 }),
  );
}

function semanticColor(color: WorldSemanticObject["color"]): number {
  return {
    red: 0xd96b68,
    blue: 0x6ca6d9,
    green: 0x72bd91,
    yellow: 0xd9b45c,
    gray: 0x87939a,
    white: 0xdfe7e8,
  }[color];
}

function makeSemanticVisual(item: WorldSemanticObject): THREE.Group {
  const group = new THREE.Group();
  group.position.set(item.center_x_m, 0, item.center_y_m);
  const material = new THREE.MeshStandardMaterial({
    color: semanticColor(item.color),
    roughness: 0.62,
    metalness: 0.08,
    transparent: item.kind === "goal_zone",
    opacity: item.kind === "goal_zone" ? 0.42 : 1,
  });

  if (item.kind === "person") {
    const body = new THREE.Mesh(new THREE.CylinderGeometry(item.size_x_m * 0.35, item.size_x_m * 0.48, item.height_m * 0.68, 12), material);
    body.position.y = item.height_m * 0.34;
    const head = new THREE.Mesh(new THREE.SphereGeometry(item.size_x_m * 0.42, 16, 10), material);
    head.position.y = item.height_m * 0.82;
    group.add(body, head);
  } else if (item.kind === "table") {
    const top = new THREE.Mesh(new THREE.BoxGeometry(item.size_x_m, item.height_m * 0.12, item.size_y_m), material);
    top.position.y = item.height_m * 0.88;
    group.add(top);
    const legMaterial = material.clone();
    for (const x of [-1, 1]) {
      for (const z of [-1, 1]) {
        const leg = new THREE.Mesh(new THREE.BoxGeometry(item.size_x_m * 0.08, item.height_m * 0.88, item.size_y_m * 0.08), legMaterial);
        leg.position.set(x * item.size_x_m * 0.38, item.height_m * 0.44, z * item.size_y_m * 0.38);
        group.add(leg);
      }
    }
  } else if (item.kind === "chair") {
    const seat = new THREE.Mesh(new THREE.BoxGeometry(item.size_x_m, item.height_m * 0.1, item.size_y_m), material);
    seat.position.y = item.height_m * 0.48;
    const back = new THREE.Mesh(new THREE.BoxGeometry(item.size_x_m, item.height_m * 0.55, item.size_y_m * 0.12), material);
    back.position.set(0, item.height_m * 0.72, item.size_y_m * 0.44);
    group.add(seat, back);
  } else if (item.kind === "door") {
    const door = new THREE.Mesh(new THREE.BoxGeometry(item.size_x_m, item.height_m, item.size_y_m), material);
    door.position.y = item.height_m / 2;
    group.add(door);
  } else if (item.kind === "bottle") {
    const bottle = new THREE.Mesh(new THREE.CylinderGeometry(item.size_x_m * 0.42, item.size_x_m * 0.5, item.height_m * 0.82, 12), material);
    bottle.position.y = item.height_m * 0.41;
    const cap = new THREE.Mesh(new THREE.CylinderGeometry(item.size_x_m * 0.25, item.size_x_m * 0.25, item.height_m * 0.18, 10), material);
    cap.position.y = item.height_m * 0.91;
    group.add(bottle, cap);
  } else {
    const zone = new THREE.Mesh(new THREE.RingGeometry(Math.min(item.size_x_m, item.size_y_m) * 0.32, Math.min(item.size_x_m, item.size_y_m) * 0.48, 32), material);
    zone.rotation.x = -Math.PI / 2;
    zone.position.y = Math.max(0.02, item.height_m);
    group.add(zone);
  }

  group.userData.semanticObjectId = item.id;
  group.userData.semanticKind = item.kind;
  group.userData.semanticSource = "simulation_ground_truth";
  return group;
}

export function createWorldVisuals(config: WorldConfig): WorldVisuals {
  const root = new THREE.Group();
  const width = config.bounds.max_x - config.bounds.min_x;
  const depth = config.bounds.max_y - config.bounds.min_y;
  const centerX = (config.bounds.min_x + config.bounds.max_x) / 2;
  const centerZ = (config.bounds.min_y + config.bounds.max_y) / 2;

  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(width, depth),
    new THREE.MeshStandardMaterial({ color: 0x1b2830, roughness: 0.86, metalness: 0.06 }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.set(centerX, 0, centerZ);
  floor.receiveShadow = true;
  root.add(floor);

  root.add(makeGrid(config));

  const boundaryMaterial = new THREE.MeshStandardMaterial({ color: 0x334c52, roughness: 0.72 });
  const boundaryHeight = 0.5;
  const boundaryThickness = 0.08;
  const boundaryPieces = [
    [width, boundaryThickness, centerX, config.bounds.min_y],
    [width, boundaryThickness, centerX, config.bounds.max_y],
  ];
  for (const [sizeX, sizeZ, x, z] of boundaryPieces) {
    const piece = new THREE.Mesh(new THREE.BoxGeometry(sizeX, boundaryHeight, sizeZ), boundaryMaterial);
    piece.position.set(x, boundaryHeight / 2, z);
    piece.castShadow = true;
    root.add(piece);
  }
  for (const x of [config.bounds.min_x, config.bounds.max_x]) {
    const piece = new THREE.Mesh(new THREE.BoxGeometry(boundaryThickness, boundaryHeight, depth), boundaryMaterial);
    piece.position.set(x, boundaryHeight / 2, centerZ);
    piece.castShadow = true;
    root.add(piece);
  }

  const obstacleMaterial = new THREE.MeshStandardMaterial({ color: 0x7a6242, roughness: 0.65 });
  for (const obstacleConfig of config.obstacles) {
    const obstacle = new THREE.Mesh(
      new THREE.BoxGeometry(obstacleConfig.size_x_m, obstacleConfig.height_m, obstacleConfig.size_y_m),
      obstacleMaterial,
    );
    obstacle.position.set(
      obstacleConfig.center_x_m,
      obstacleConfig.height_m / 2,
      obstacleConfig.center_y_m,
    );
    obstacle.castShadow = true;
    obstacle.receiveShadow = true;
    obstacle.userData.worldObstacleId = obstacleConfig.id;
    root.add(obstacle);
  }

  for (const semanticObject of config.semantic_objects) {
    root.add(makeSemanticVisual(semanticObject));
  }

  const rays = [
    makeRay(0xe06c75),
    makeRay(0x72c99b),
    makeRay(0x91b8c2),
  ];
  rays.forEach(({ line }) => root.add(line));

  return {
    root,
    update(frame) {
      setRay(rays[0], frame, frame.robot.yaw_deg, frame.sensors.front_distance_cm);
      setRay(rays[1], frame, frame.robot.yaw_deg + 90, frame.sensors.left_distance_cm);
      setRay(rays[2], frame, frame.robot.yaw_deg - 90, frame.sensors.right_distance_cm);
    },
  };
}
