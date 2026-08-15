import assert from "node:assert/strict";
import { createReadStream } from "node:fs";
import { createServer, type Server } from "node:http";
import path from "node:path";
import test from "node:test";

import { loadGo1Visual } from "../src/go1_robot.ts";
import type { SimulationFrame } from "../src/simulation.ts";

if (typeof globalThis.ProgressEvent === "undefined") {
  class NodeProgressEvent extends Event {
    readonly lengthComputable: boolean;
    readonly loaded: number;
    readonly total: number;

    constructor(type: string, init: ProgressEventInit = {}) {
      super(type);
      this.lengthComputable = init.lengthComputable ?? false;
      this.loaded = init.loaded ?? 0;
      this.total = init.total ?? 0;
    }
  }
  Object.defineProperty(globalThis, "ProgressEvent", { value: NodeProgressEvent });
}

const assetRoot = path.resolve("public/assets/go1");
const assetNames = ["trunk.stl", "hip.stl", "thigh.stl", "thigh_mirror.stl", "calf.stl"];

function frame(overrides: Partial<SimulationFrame["robot"]> = {}): SimulationFrame {
  return {
    type: "simulation.frame",
    version: 1,
    revision: 1,
    sim_time_ms: 50,
    robot: {
      x_m: 0,
      y_m: 0,
      yaw_deg: 0,
      linear_speed_mps: 0,
      angular_speed_dps: 0,
      gait: "stand",
      gait_phase: 0,
      emergency_stopped: false,
      ...overrides,
    },
    sensors: {
      front_distance_cm: 1_000,
      left_distance_cm: 1_000,
      right_distance_cm: 1_000,
    },
    active_command: null,
  };
}

async function closeServer(server: Server): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  });
}

async function withAssetServer<T>(
  missing: Set<string>,
  run: (baseUrl: string, requests: Map<string, number>) => Promise<T>,
  corrupt: Set<string> = new Set(),
): Promise<T> {
  const requests = new Map<string, number>();
  const server = createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url ?? "/", "http://127.0.0.1").pathname);
    const file = pathname.slice(1);
    requests.set(file, (requests.get(file) ?? 0) + 1);
    if (!assetNames.includes(file) || missing.has(file)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    response.setHeader("Content-Type", "model/stl");
    if (corrupt.has(file)) {
      response.end("this is not an STL mesh");
      return;
    }
    createReadStream(path.join(assetRoot, file))
      .on("error", () => {
        response.statusCode = 500;
        response.end();
      })
      .pipe(response);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert(address && typeof address === "object");
  try {
    return await run(`http://127.0.0.1:${address.port}`, requests);
  } finally {
    await closeServer(server);
  }
}

test("loads each GO1 STL once and builds a shared source-space hierarchy", async () => {
  await withAssetServer(new Set(), async (baseUrl, requests) => {
    const controller = await loadGo1Visual({ assetBaseUrl: baseUrl });
    assert.equal(controller.state, "ready");
    assert.equal(controller.error, null);

    const meshes: Array<{ geometry: unknown }> = [];
    controller.root.traverse((object) => {
      if ("isMesh" in object && object.isMesh) meshes.push(object as unknown as { geometry: unknown });
    });
    assert.equal(meshes.length, 17);
    assert.equal(new Set(meshes.map((mesh) => mesh.geometry)).size, 6);
    for (const asset of assetNames) assert.equal(requests.get(asset), 1);

    const adapter = controller.root.getObjectByName("go1-source-space-adapter");
    assert(adapter);
    assert(Math.abs(adapter.rotation.x + Math.PI / 2) < 1e-9);
    for (const leg of ["FR", "FL", "RR", "RL"]) {
      const foot = controller.root.getObjectByName(`${leg}-foot-pivot`);
      assert(foot);
      assert.equal(foot.parent?.name, `${leg}-calf-pivot`);
      assert.equal(foot.children[0]?.name, `${leg}-foot`);
    }

    controller.dispose();
    assert.equal(controller.root.children.length, 0);
  });
});

test("maps authoritative frames to deterministic stand, trot, and turn poses", async () => {
  await withAssetServer(new Set(), async (baseUrl) => {
    const controller = await loadGo1Visual({ assetBaseUrl: baseUrl });
    const stand = frame({ x_m: 2, y_m: 1, yaw_deg: 90 });
    controller.update(stand, stand, 1);
    assert.equal(controller.root.position.x, 2);
    assert.equal(controller.root.position.z, 1);
    assert(Math.abs(controller.root.rotation.y + Math.PI / 2) < 1e-9);
    assert.equal(controller.root.getObjectByName("FR-thigh-pivot")?.rotation.y, 0.9);
    assert.equal(controller.root.getObjectByName("FR-calf-pivot")?.rotation.y, -1.8);

    const walk = frame({ gait: "walk", gait_phase: 0.25, linear_speed_mps: 0.5 });
    controller.update(walk, walk, 1);
    const frontRightWalk = controller.root.getObjectByName("FR-thigh-pivot")!.rotation.y;
    const rearLeftWalk = controller.root.getObjectByName("RL-thigh-pivot")!.rotation.y;
    const frontLeftWalk = controller.root.getObjectByName("FL-thigh-pivot")!.rotation.y;
    assert(frontRightWalk > 0.9);
    assert(frontLeftWalk < 0.9);
    assert(Math.abs(frontRightWalk - rearLeftWalk) < 1e-9);

    const turn = frame({ gait: "turn", gait_phase: 0.25, angular_speed_dps: 90 });
    controller.update(turn, turn, 1);
    const frontRightTurn = controller.root.getObjectByName("FR-thigh-pivot")!.rotation.y;
    const frontLeftTurn = controller.root.getObjectByName("FL-thigh-pivot")!.rotation.y;
    assert(Math.abs(frontLeftTurn - 0.9) > Math.abs(frontRightTurn - 0.9));
    controller.dispose();
  });
});

test("interpolates root pose between adjacent authoritative frames", async () => {
  await withAssetServer(new Set(), async (baseUrl) => {
    const controller = await loadGo1Visual({ assetBaseUrl: baseUrl });
    const previous = frame({ x_m: 0, y_m: 0, yaw_deg: 0 });
    const latest = frame({ x_m: 4, y_m: 2, yaw_deg: 90 });

    controller.update(previous, latest, 0.25);

    assert.equal(controller.root.position.x, 1);
    assert.equal(controller.root.position.z, 0.5);
    assert(Math.abs(controller.root.rotation.y + Math.PI / 8) < 1e-9);
    controller.dispose();
  });
});

test("returns stopped and emergency frames to a stable home pose", async () => {
  await withAssetServer(new Set(), async (baseUrl) => {
    const controller = await loadGo1Visual({ assetBaseUrl: baseUrl });
    const walk = frame({ gait: "walk", gait_phase: 0.25, linear_speed_mps: 0.5 });
    controller.update(walk, walk, 1);
    assert.notEqual(controller.root.getObjectByName("FR-thigh-pivot")?.rotation.y, 0.9);

    const stopped = frame({ gait: "stopped" });
    controller.update(stopped, stopped, 1);
    for (const leg of ["FR", "FL", "RR", "RL"]) {
      assert.equal(controller.root.getObjectByName(`${leg}-hip-pivot`)?.rotation.x, 0);
      assert.equal(controller.root.getObjectByName(`${leg}-thigh-pivot`)?.rotation.y, 0.9);
      assert.equal(controller.root.getObjectByName(`${leg}-calf-pivot`)?.rotation.y, -1.8);
    }

    const emergency = frame({ gait: "stopped", emergency_stopped: true });
    controller.update(emergency, emergency, 1);
    assert.equal(controller.root.position.y, 0);
    assert.equal(controller.root.getObjectByName("FR-thigh-pivot")?.rotation.y, 0.9);
    controller.dispose();
  });
});

test("reports a missing required STL as failed without a partial robot", async () => {
  await withAssetServer(new Set(["calf.stl"]), async (baseUrl, requests) => {
    const controller = await loadGo1Visual({ assetBaseUrl: baseUrl });
    assert.equal(controller.state, "failed");
    assert.match(controller.error ?? "", /calf\.stl/);
    assert.equal(controller.root.visible, false);
    assert.equal(controller.root.children.length, 0);
    for (const asset of assetNames) assert.equal(requests.get(asset), 1);
    controller.dispose();
  });
});

test("reports an unparseable STL as failed without a partial robot", async () => {
  await withAssetServer(new Set(), async (baseUrl, requests) => {
    const controller = await loadGo1Visual({ assetBaseUrl: baseUrl });
    assert.equal(controller.state, "failed");
    assert.match(controller.error ?? "", /thigh\.stl:/);
    assert.equal(controller.root.visible, false);
    assert.equal(controller.root.children.length, 0);
    for (const asset of assetNames) assert.equal(requests.get(asset), 1);
    controller.dispose();
  }, new Set(["thigh.stl"]));
});
