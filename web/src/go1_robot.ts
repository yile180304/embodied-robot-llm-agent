import * as THREE from "three";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { toThreePose, type SimulationFrame } from "./simulation.ts";

export type Go1AssetState = "loading" | "ready" | "failed";

export type Go1VisualController = {
  root: THREE.Group;
  readonly state: Go1AssetState;
  readonly error: string | null;
  update(previous: SimulationFrame, latest: SimulationFrame, interpolation: number): void;
  dispose(): void;
};

type Go1AssetGeometrySet = {
  trunk: THREE.BufferGeometry;
  hip: THREE.BufferGeometry;
  thigh: THREE.BufferGeometry;
  thighMirror: THREE.BufferGeometry;
  calf: THREE.BufferGeometry;
};

type Go1GeometrySet = Go1AssetGeometrySet & {
  foot: THREE.BufferGeometry;
};

type Go1Leg = {
  side: -1 | 1;
  hip: THREE.Group;
  thigh: THREE.Group;
  calf: THREE.Group;
  foot: THREE.Group;
};

const ASSET_FILES = {
  trunk: "trunk.stl",
  hip: "hip.stl",
  thigh: "thigh.stl",
  thighMirror: "thigh_mirror.stl",
  calf: "calf.stl",
} as const;

const HOME_HIP = 0;
const HOME_THIGH = 0.9;
const HOME_CALF = -1.8;
const VISUAL_TRUNK_HEIGHT = 0.27;
const TAU = Math.PI * 2;

function joinAssetUrl(baseUrl: string, file: string): string {
  return `${baseUrl.replace(/\/+$/, "")}/${file}`;
}

function validateGeometry(file: string, geometry: THREE.BufferGeometry): THREE.BufferGeometry {
  const position = geometry.getAttribute("position");
  if (!position || position.count < 3) {
    geometry.dispose();
    throw new Error(`${file} contains no triangles`);
  }
  geometry.computeBoundingBox();
  const bounds = geometry.boundingBox;
  if (!bounds || !bounds.min.toArray().every(Number.isFinite) || !bounds.max.toArray().every(Number.isFinite)) {
    geometry.dispose();
    throw new Error(`${file} has invalid bounds`);
  }
  geometry.computeVertexNormals();
  return geometry;
}

function createMaterial(
  color: number,
  roughness: number,
  metalness: number,
  materials: Set<THREE.Material>,
): THREE.MeshStandardMaterial {
  const material = new THREE.MeshStandardMaterial({ color, roughness, metalness });
  materials.add(material);
  return material;
}

function createMesh(
  geometry: THREE.BufferGeometry,
  material: THREE.Material,
  name: string,
): THREE.Mesh {
  const mesh = new THREE.Mesh(geometry, material);
  mesh.name = name;
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function makeLeg(
  definition: {
    name: string;
    x: number;
    y: number;
    side: -1 | 1;
    hipVisualQuaternion: THREE.Quaternion;
    thighMirror: boolean;
  },
  geometry: Go1GeometrySet,
  materials: {
    hip: THREE.Material;
    thigh: THREE.Material;
    calf: THREE.Material;
    foot: THREE.Material;
  },
): Go1Leg {
  const hip = new THREE.Group();
  hip.name = `${definition.name}-hip-pivot`;
  hip.position.set(definition.x, definition.y, 0);
  const hipVisual = createMesh(geometry.hip, materials.hip, `${definition.name}-hip`);
  hipVisual.quaternion.copy(definition.hipVisualQuaternion);
  hip.add(hipVisual);

  const thigh = new THREE.Group();
  thigh.name = `${definition.name}-thigh-pivot`;
  thigh.position.set(0, definition.side * 0.08, 0);
  thigh.rotation.y = HOME_THIGH;
  thigh.add(createMesh(
    definition.thighMirror ? geometry.thighMirror : geometry.thigh,
    materials.thigh,
    `${definition.name}-thigh`,
  ));
  hip.add(thigh);

  const calf = new THREE.Group();
  calf.name = `${definition.name}-calf-pivot`;
  calf.position.set(0, 0, -0.213);
  calf.rotation.y = HOME_CALF;
  calf.add(createMesh(geometry.calf, materials.calf, `${definition.name}-calf`));
  thigh.add(calf);

  const foot = new THREE.Group();
  foot.name = `${definition.name}-foot-pivot`;
  foot.position.set(0, 0, -0.213);
  foot.add(createMesh(geometry.foot, materials.foot, `${definition.name}-foot`));
  calf.add(foot);

  return { side: definition.side, hip, thigh, calf, foot };
}

function disposeResources(
  geometries: Iterable<THREE.BufferGeometry>,
  materials: Iterable<THREE.Material>,
): void {
  for (const geometry of geometries) geometry.dispose();
  for (const material of materials) material.dispose();
}

function failedController(error: string, geometries: Iterable<THREE.BufferGeometry> = []): Go1VisualController {
  const root = new THREE.Group();
  root.name = "go1-visual-failed";
  root.visible = false;
  let disposed = false;
  return {
    root,
    state: "failed",
    error,
    update: () => undefined,
    dispose: () => {
      if (disposed) return;
      disposed = true;
      disposeResources(geometries, []);
      root.clear();
    },
  };
}

function createReadyController(assetGeometry: Go1AssetGeometrySet): Go1VisualController {
  const root = new THREE.Group();
  root.name = "go1-visual-root";

  const geometry: Go1GeometrySet = {
    ...assetGeometry,
    // go1.xml defines each rubber foot as a 0.023 m sphere rather than an STL mesh.
    foot: new THREE.SphereGeometry(0.023, 16, 12),
  };

  const materials = new Set<THREE.Material>();
  const trunkMaterial = createMaterial(0xaeb8b9, 0.5, 0.24, materials);
  const hipMaterial = createMaterial(0x303a3f, 0.42, 0.48, materials);
  const thighMaterial = createMaterial(0x87979a, 0.52, 0.28, materials);
  const calfMaterial = createMaterial(0x2e373b, 0.64, 0.24, materials);
  const footMaterial = createMaterial(0x171d20, 0.82, 0.12, materials);

  // The source MJCF is Z-up. Keep all source-space joints under one adapter
  // so the Z-up -> Three.js Y-up conversion happens exactly once.
  const assetRoot = new THREE.Group();
  assetRoot.name = "go1-source-space-adapter";
  assetRoot.position.y = VISUAL_TRUNK_HEIGHT;
  assetRoot.rotation.x = -Math.PI / 2;
  root.add(assetRoot);

  assetRoot.add(createMesh(geometry.trunk, trunkMaterial, "go1-trunk"));
  const legs: Go1Leg[] = [
    makeLeg({
      name: "FR",
      x: 0.1881,
      y: -0.04675,
      side: -1,
      hipVisualQuaternion: new THREE.Quaternion(0, 0, 0, 1),
      thighMirror: true,
    }, geometry, { hip: hipMaterial, thigh: thighMaterial, calf: calfMaterial, foot: footMaterial }),
    makeLeg({
      name: "FL",
      x: 0.1881,
      y: 0.04675,
      side: 1,
      hipVisualQuaternion: new THREE.Quaternion(0, 0, 0, 1),
      thighMirror: false,
    }, geometry, { hip: hipMaterial, thigh: thighMaterial, calf: calfMaterial, foot: footMaterial }),
    makeLeg({
      name: "RR",
      x: -0.1881,
      y: -0.04675,
      side: -1,
      hipVisualQuaternion: new THREE.Quaternion(0, 0, -1, 0),
      thighMirror: true,
    }, geometry, { hip: hipMaterial, thigh: thighMaterial, calf: calfMaterial, foot: footMaterial }),
    makeLeg({
      name: "RL",
      x: -0.1881,
      y: 0.04675,
      side: 1,
      hipVisualQuaternion: new THREE.Quaternion(0, 1, 0, 0),
      thighMirror: false,
    }, geometry, { hip: hipMaterial, thigh: thighMaterial, calf: calfMaterial, foot: footMaterial }),
  ];
  for (const leg of legs) assetRoot.add(leg.hip);

  let disposed = false;
  return {
    root,
    state: "ready",
    error: null,
    update(previous, latest, interpolation) {
      if (disposed) return;
      const previousPose = toThreePose(previous);
      const latestPose = toThreePose(latest);
      const locomoting = latest.robot.gait === "walk" || latest.robot.gait === "turn";
      const phase = latest.robot.gait_phase * TAU;
      const turnFactor = latest.robot.gait === "turn"
        ? THREE.MathUtils.clamp(latest.robot.angular_speed_dps / 90, -1, 1)
        : 0;

      root.position.x = THREE.MathUtils.lerp(previousPose.x, latestPose.x, interpolation);
      root.position.z = THREE.MathUtils.lerp(previousPose.z, latestPose.z, interpolation);
      root.position.y = locomoting ? Math.abs(Math.sin(phase)) * 0.025 : 0;
      root.rotation.y = THREE.MathUtils.lerp(previousPose.yawRadians, latestPose.yawRadians, interpolation);

      legs.forEach((leg, index) => {
        if (!locomoting) {
          leg.hip.rotation.x = HOME_HIP;
          leg.thigh.rotation.y = HOME_THIGH;
          leg.calf.rotation.y = HOME_CALF;
          return;
        }
        const diagonalOffset = index === 0 || index === 3 ? 0 : Math.PI;
        const stride = Math.sin(phase + diagonalOffset);
        const turnScale = latest.robot.gait === "turn"
          ? 1 + leg.side * turnFactor * 0.45
          : 1;
        leg.hip.rotation.x = HOME_HIP + leg.side * stride * 0.08 * turnScale;
        leg.thigh.rotation.y = HOME_THIGH + stride * 0.24 * turnScale;
        leg.calf.rotation.y = HOME_CALF - stride * 0.34 * turnScale;
      });
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      disposeResources(new Set(Object.values(geometry)), materials);
      root.clear();
    },
  };
}

export async function loadGo1Visual(options: { assetBaseUrl?: string } = {}): Promise<Go1VisualController> {
  const assetBaseUrl = options.assetBaseUrl ?? "/assets/go1/";
  const loader = new STLLoader();
  const entries = Object.entries(ASSET_FILES) as Array<[keyof Go1AssetGeometrySet, string]>;
  const results = await Promise.allSettled(entries.map(async ([key, file]) => {
    const geometry = await loader.loadAsync(joinAssetUrl(assetBaseUrl, file));
    return [key, validateGeometry(file, geometry)] as const;
  }));

  const geometries = new Map<keyof Go1AssetGeometrySet, THREE.BufferGeometry>();
  const failures: string[] = [];
  results.forEach((result, index) => {
    const [key, file] = entries[index];
    if (result.status === "fulfilled") {
      geometries.set(result.value[0], result.value[1]);
    } else {
      const reason = result.reason instanceof Error ? result.reason.message : String(result.reason);
      failures.push(`${file}: ${reason}`);
    }
  });

  if (failures.length > 0) {
    return failedController(
      `GO1 visual assets failed to load: ${failures.join("; ")}`,
      geometries.values(),
    );
  }

  return createReadyController({
    trunk: geometries.get("trunk")!,
    hip: geometries.get("hip")!,
    thigh: geometries.get("thigh")!,
    thighMirror: geometries.get("thighMirror")!,
    calf: geometries.get("calf")!,
  });
}
