import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import {
  createIcons,
  Download,
  FlaskConical,
  LogOut,
  OctagonX,
  Pause,
  Play,
  RotateCcw,
  Send,
  SkipBack,
  SkipForward,
  Upload,
  X,
} from "lucide";
import { createFaultConsole } from "./fault_console";
import { parseFaultRunSnapshot, type FaultRunSnapshot, type FaultScenario } from "./fault";
import { createMissionConsole } from "./mission_console";
import {
  acceptSimulationFrame,
  initialSimulationViewState,
  parseSimulationFrame,
  type SimulationFrame,
} from "./simulation";
import {
  parseMissionTaskSnapshot,
  parseRuntimeApiError,
  parseRuntimeCapabilities,
  parseRuntimeEvent,
  type MissionTaskRequest,
  type RuntimeCapabilities,
} from "./mission";
import { parseWorldConfig, type WorldConfig } from "./world";
import { createWorldVisuals, type WorldVisuals } from "./world_renderer";
import { loadGo1Visual, type Go1VisualController } from "./go1_robot";
import { createReplayConsole } from "./replay_console";
import {
  parseReplayBundle,
  projectReplay,
  type ReplayBundle,
  type ReplayPlaybackState,
} from "./replay";
import "./style.css";

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`missing required element: ${selector}`);
  return element;
}

const app = requiredElement<HTMLDivElement>("#app");

app.innerHTML = `
  <main class="runtime-shell" data-connection="connecting">
    <header class="topbar">
      <div>
        <p class="eyebrow">EMBODIED AGENT / SIMULATION</p>
        <h1>Quadruped Runtime</h1>
      </div>
      <div class="topbar-meta">
        <span class="status-dot" aria-hidden="true"></span>
        <span id="connection-label">Connecting</span>
        <code>SIM FRAME v1</code>
      </div>
    </header>
    <section class="workspace" aria-label="四足机器人仿真工作区">
      <section class="mission-column" aria-label="任务控制台">
        <div id="mission-console"></div>
        <div id="fault-console"></div>
      </section>
      <div class="scene-column">
        <div class="scene-toolbar">
          <div>
            <span class="section-label">WORLD</span>
            <strong id="world-mode">Indoor Lab / Authoritative</strong>
          </div>
          <div class="scene-toolbar-meta">
            <span id="asset-status" class="asset-status" data-state="loading" title="Loading local GO1 visual assets">GO1 / LOADING</span>
            <span class="frame-readout">REV <b id="revision">0000</b></span>
          </div>
        </div>
        <div id="scene" class="scene" role="img" aria-label="四足机器人三维仿真场景"></div>
        <div class="scene-footer">
          <span id="semantic-world-badge" class="semantic-world-badge" hidden>simulation ground truth</span>
          <span><i class="legend-swatch robot-swatch"></i>robot</span>
          <span><i class="legend-swatch front-ray-swatch"></i>front ray</span>
          <span><i class="legend-swatch left-ray-swatch"></i>left ray</span>
          <span><i class="legend-swatch right-ray-swatch"></i>right ray</span>
        </div>
      </div>
      <aside class="status-column" aria-label="仿真状态">
        <div class="panel-heading">
          <span class="section-label">RUNTIME</span>
          <span class="live-label"><i class="status-dot" aria-hidden="true"></i><span id="connection-badge">CONNECTING</span></span>
        </div>
        <dl class="telemetry-list">
          <div><dt>Pose</dt><dd id="pose">0.00, 0.00 m</dd></div>
          <div><dt>Heading</dt><dd id="heading">0.0°</dd></div>
          <div><dt>Gait</dt><dd id="gait">stand</dd></div>
          <div><dt>Logic time</dt><dd id="sim-time">0.00 s</dd></div>
          <div><dt>Front</dt><dd id="front-distance">0 cm</dd></div>
          <div><dt>Left / Right</dt><dd id="side-distance">0 / 0 cm</dd></div>
        </dl>
        <div class="control-group">
          <span class="section-label">OPERATOR</span>
          <div class="control-row">
            <button class="icon-button" id="pause" type="button" aria-label="暂停仿真" data-tooltip="Pause simulation">
              <i data-lucide="pause" aria-hidden="true"></i>
            </button>
            <button class="icon-button" id="resume" type="button" aria-label="继续仿真" data-tooltip="Resume simulation">
              <i data-lucide="play" aria-hidden="true"></i>
            </button>
            <button class="icon-button" id="reset" type="button" aria-label="重置仿真" data-tooltip="Reset simulation">
              <i data-lucide="rotate-ccw" aria-hidden="true"></i>
            </button>
            <button class="icon-button danger-button" id="emergency-stop" type="button" aria-label="紧急停止" data-tooltip="Emergency stop" disabled>
              <i data-lucide="octagon-x" aria-hidden="true"></i>
            </button>
          </div>
        </div>
        <div class="next-step">
          <span class="section-label">ACTIVE COMMAND</span>
          <strong id="active-command">Idle / waiting for command</strong>
          <p id="active-progress">No motion is active in the authoritative world.</p>
        </div>
      </aside>
    </section>
    <section class="replay-band" aria-label="回放与证据工作台">
      <div id="replay-console"></div>
    </section>
  </main>
`;

const missionConsole = createMissionConsole(
  requiredElement<HTMLElement>("#mission-console"),
  {
    onSubmit: submitMission,
    onCancel: cancelMission,
  },
);
const faultConsole = createFaultConsole(
  requiredElement<HTMLElement>("#fault-console"),
  {
    onRun: submitFault,
  },
);
const replayConsole = createReplayConsole(
  requiredElement<HTMLElement>("#replay-console"),
  {
    onExport: exportReplay,
    onEnter: enterReplay,
    onProjection: projectReplayView,
    onExit: exitReplay,
  },
);

createIcons({
  icons: {
    Download,
    FlaskConical,
    LogOut,
    OctagonX,
    Pause,
    Play,
    RotateCcw,
    Send,
    SkipBack,
    SkipForward,
    Upload,
    X,
  },
  attrs: { width: 17, height: 17 },
});

const scene = requiredElement<HTMLDivElement>("#scene");
const runtimeShell = requiredElement<HTMLElement>(".runtime-shell");
const connectionLabel = requiredElement<HTMLElement>("#connection-label");
const connectionBadge = requiredElement<HTMLElement>("#connection-badge");
const revision = requiredElement<HTMLElement>("#revision");
const pose = requiredElement<HTMLElement>("#pose");
const heading = requiredElement<HTMLElement>("#heading");
const gait = requiredElement<HTMLElement>("#gait");
const simTime = requiredElement<HTMLElement>("#sim-time");
const frontDistance = requiredElement<HTMLElement>("#front-distance");
const sideDistance = requiredElement<HTMLElement>("#side-distance");
const worldMode = requiredElement<HTMLElement>("#world-mode");
const semanticWorldBadge = requiredElement<HTMLElement>("#semantic-world-badge");
const assetStatus = requiredElement<HTMLElement>("#asset-status");
const activeCommand = requiredElement<HTMLElement>("#active-command");
const activeProgress = requiredElement<HTMLElement>("#active-progress");
const pauseButton = requiredElement<HTMLButtonElement>("#pause");
const resumeButton = requiredElement<HTMLButtonElement>("#resume");
const resetButton = requiredElement<HTMLButtonElement>("#reset");
const emergencyButton = requiredElement<HTMLButtonElement>("#emergency-stop");

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0x10161d, 1);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
scene.appendChild(renderer.domElement);

const world = new THREE.Scene();
world.fog = new THREE.Fog(0x10161d, 12, 30);
const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
camera.position.set(3.8, 3, 4.4);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.target.set(0, 0.24, 0);
controls.minDistance = 2.5;
controls.maxDistance = 12;

world.add(new THREE.HemisphereLight(0xaec7d8, 0x10161d, 1.7));
const keyLight = new THREE.DirectionalLight(0xffe8c7, 3.2);
keyLight.position.set(4, 8, 3);
keyLight.castShadow = true;
keyLight.shadow.mapSize.set(2048, 2048);
world.add(keyLight);

let robot: Go1VisualController | null = null;
let liveViewState = initialSimulationViewState;
let worldVisuals: WorldVisuals | null = null;
let loadedWorld: WorldConfig | null = null;
let liveWorld: WorldConfig | null = null;
let displayedFrame: SimulationFrame | null = null;
let displayedPreviousFrame: SimulationFrame | null = null;
let displayedFrameReceivedAt = performance.now();
let replayMode = false;
let replayBundle: ReplayBundle | null = null;
let replayTimelineCount = -1;
let replayFrameRevision = -1;
let reconnectAttempt = 0;
let reconnectTimer: number | null = null;
let runtimeCapabilities: RuntimeCapabilities | null = null;
let faultPollTimer: number | null = null;
const reconnectDelaysMs = [250, 500, 1_000, 2_000, 4_000];

const connectionText = {
  connecting: { label: "Connecting", badge: "CONNECTING" },
  live: { label: "Simulation live", badge: "LIVE" },
  reconnecting: { label: "Reconnecting", badge: "RECONNECTING" },
  offline: { label: "Simulation offline", badge: "OFFLINE" },
} as const;

function renderRuntimeMode(): void {
  runtimeShell.dataset.mode = replayMode ? "replay" : "live";
  if (replayMode) {
    connectionLabel.textContent = "Local replay";
    connectionBadge.textContent = "REPLAY";
    return;
  }
  const text = connectionText[liveViewState.connection];
  connectionLabel.textContent = text.label;
  connectionBadge.textContent = text.badge;
}

function setConnection(connection: typeof liveViewState.connection) {
  liveViewState = { ...liveViewState, connection };
  runtimeShell.dataset.connection = connection;
  renderRuntimeMode();
}

function setAssetStatus(state: "loading" | "ready" | "failed", error: string | null = null) {
  assetStatus.dataset.state = state;
  assetStatus.textContent = `GO1 / ${state.toUpperCase()}`;
  assetStatus.title = error ?? `Local GO1 visual assets ${state}.`;
  if (state === "failed") {
    console.warn("GO1 visual assets failed", error);
  }
}

function updateReadouts(frame: SimulationFrame) {
  revision.textContent = String(frame.revision).padStart(4, "0");
  pose.textContent = `${frame.robot.x_m.toFixed(2)}, ${frame.robot.y_m.toFixed(2)} m`;
  heading.textContent = `${frame.robot.yaw_deg.toFixed(1)}°`;
  gait.textContent = frame.robot.gait;
  simTime.textContent = `${(frame.sim_time_ms / 1_000).toFixed(2)} s`;
  frontDistance.textContent = `${frame.sensors.front_distance_cm.toFixed(0)} cm`;
  sideDistance.textContent = `${frame.sensors.left_distance_cm.toFixed(0)} / ${frame.sensors.right_distance_cm.toFixed(0)} cm`;
  if (frame.active_command) {
    const command = frame.active_command;
    const preview = command.task_id === "demo-program";
    worldMode.textContent = `${loadedWorld?.scene_id ?? "World"} / ${replayMode ? "Replay" : preview ? "Preview" : "Agent + MQTT"}`;
    activeCommand.textContent = `${command.tool} · ${command.task_id} #${command.seq}`;
    activeProgress.textContent = `Authoritative progress ${(command.progress * 100).toFixed(0)}%`;
  } else {
    activeCommand.textContent = "Idle / waiting for command";
    activeProgress.textContent = frame.robot.emergency_stopped
      ? "Emergency stop is active; motion commands are disabled."
      : "No motion is active in the authoritative world.";
  }
}

async function loadRuntimeCapabilities(): Promise<void> {
  const response = await fetch("/api/runtime/capabilities");
  if (!response.ok) throw new Error(`runtime capabilities failed with HTTP ${response.status}`);
  const capabilities = parseRuntimeCapabilities(await response.json());
  if (!capabilities) throw new Error("runtime returned invalid capabilities");
  runtimeCapabilities = capabilities;
  missionConsole.setCapabilities(capabilities);
  faultConsole.setCapabilities(capabilities);
  replayConsole.setExportAvailable(capabilities.mode === "bridge");
  updateMutationControls();
  if (capabilities.fault_injection) await loadCurrentFault();
}

async function loadCurrentTask(): Promise<void> {
  if (replayMode) return;
  const response = await fetch("/api/tasks/current");
  if (response.status === 204) {
    missionConsole.setSnapshot(null);
    faultConsole.setMissionActive(false);
    return;
  }
  if (!response.ok) throw new Error(`current mission failed with HTTP ${response.status}`);
  const snapshot = parseMissionTaskSnapshot(await response.json());
  if (!snapshot) throw new Error("runtime returned an invalid mission snapshot");
  missionConsole.setSnapshot(snapshot);
  faultConsole.setMissionActive(snapshot.status === "accepted" || snapshot.status === "running");
}

async function loadCurrentFault(): Promise<FaultRunSnapshot | null> {
  if (replayMode) return null;
  const response = await fetch("/api/faults/current");
  if (response.status === 204) {
    faultConsole.setSnapshot(null);
    missionConsole.setFaultActive(false);
    return null;
  }
  if (!response.ok) throw new Error(`current fault failed with HTTP ${response.status}`);
  const snapshot = parseFaultRunSnapshot(await response.json());
  if (!snapshot) throw new Error("runtime returned an invalid fault snapshot");
  faultConsole.setSnapshot(snapshot);
  const active = snapshot.status === "accepted" || snapshot.status === "running";
  missionConsole.setFaultActive(active);
  if (active) scheduleFaultPoll();
  return snapshot;
}

async function submitMission(request: MissionTaskRequest): Promise<void> {
  if (replayMode) throw new Error("Exit Replay Mode before submitting a mission.");
  const response = await fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    const error = parseRuntimeApiError(await response.json().catch(() => null));
    throw new Error(error ? `${error.error_code}: ${error.error_message}` : `mission failed with HTTP ${response.status}`);
  }
  const accepted = await response.json() as { status?: unknown };
  if (accepted.status !== "accepted") throw new Error("runtime did not accept the mission");
  await loadCurrentTask();
}

async function cancelMission(): Promise<void> {
  if (replayMode) throw new Error("Exit Replay Mode before cancelling a mission.");
  const response = await fetch("/api/tasks/current/cancel", { method: "POST" });
  if (!response.ok) {
    const error = parseRuntimeApiError(await response.json().catch(() => null));
    throw new Error(error ? `${error.error_code}: ${error.error_message}` : `cancel failed with HTTP ${response.status}`);
  }
  const accepted = await response.json() as { status?: unknown };
  if (accepted.status !== "cancel_requested") throw new Error("runtime did not accept mission cancellation");
  await loadCurrentTask();
}

async function submitFault(scenario: FaultScenario): Promise<void> {
  if (replayMode) throw new Error("Exit Replay Mode before starting a fault run.");
  const response = await fetch("/api/faults", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario }),
  });
  if (!response.ok) {
    const error = parseRuntimeApiError(await response.json().catch(() => null));
    throw new Error(error ? `${error.error_code}: ${error.error_message}` : `fault failed with HTTP ${response.status}`);
  }
  const accepted = await response.json() as { status?: unknown };
  if (accepted.status !== "accepted") throw new Error("runtime did not accept the fault run");
  await loadCurrentFault();
  scheduleFaultPoll();
}

function scheduleFaultPoll(): void {
  if (faultPollTimer !== null) return;
  faultPollTimer = window.setTimeout(() => {
    faultPollTimer = null;
    void loadCurrentFault().catch((error) => {
      faultConsole.setError(error instanceof Error ? error.message : "Fault status unavailable.");
    });
  }, 250);
}

async function runEmergencyStop(): Promise<void> {
  if (replayMode) return;
  emergencyButton.disabled = true;
  missionConsole.setError(null);
  try {
    const response = await fetch("/api/simulation/emergency-stop", { method: "POST" });
    if (!response.ok) {
      const error = parseRuntimeApiError(await response.json().catch(() => null));
      throw new Error(error ? `${error.error_code}: ${error.error_message}` : `emergency stop failed with HTTP ${response.status}`);
    }
    const observation = await response.json() as { status?: unknown };
    if (observation.status !== "emergency_stop" && observation.status !== "success") {
      throw new Error("runtime returned an invalid emergency stop Observation");
    }
    await loadCurrentTask();
  } catch (error) {
    missionConsole.setError(error instanceof Error ? error.message : "Emergency stop failed.");
  } finally {
    updateMutationControls();
  }
}

function displayFrame(frame: SimulationFrame, previous: SimulationFrame = frame): void {
  displayedPreviousFrame = previous;
  displayedFrame = frame;
  displayedFrameReceivedAt = performance.now();
  worldVisuals?.update(frame);
  updateReadouts(frame);
}

function receiveLiveFrame(frame: SimulationFrame): void {
  const nextState = acceptSimulationFrame(liveViewState, frame);
  if (nextState === liveViewState) return;
  liveViewState = nextState;
  if (!replayMode && nextState.latestFrame) {
    displayFrame(nextState.latestFrame, nextState.previousFrame ?? nextState.latestFrame);
  }
}

function connectSimulationStream() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  let socket: WebSocket;
  setConnection(reconnectAttempt === 0 ? "connecting" : "reconnecting");
  try {
    socket = new WebSocket(`${protocol}//${window.location.host}/ws/events`);
  } catch {
    scheduleReconnect();
    return;
  }
  socket.addEventListener("open", () => {
    reconnectAttempt = 0;
    setConnection("live");
    if (!replayMode) void loadCurrentTask().catch((error) => missionConsole.setError(String(error)));
    if (!replayMode && runtimeCapabilities?.fault_injection) {
      void loadCurrentFault().catch((error) => faultConsole.setError(String(error)));
    }
  });
  socket.addEventListener("message", (event) => {
    try {
      const payload: unknown = JSON.parse(String(event.data));
      const frame = parseSimulationFrame(payload);
      if (frame) {
        receiveLiveFrame(frame);
        return;
      }
      const runtimeEvent = parseRuntimeEvent(payload);
      if (runtimeEvent) {
        if (replayMode) return;
        missionConsole.acceptEvent(runtimeEvent);
        if (runtimeEvent.phase === "user_goal") faultConsole.setMissionActive(true);
        if (runtimeEvent.phase === "final") {
          void loadCurrentTask().catch((error) => missionConsole.setError(String(error)));
        }
        return;
      }
      console.warn("ignored invalid runtime message", payload);
    } catch (error) {
      console.warn("ignored invalid runtime message", error);
    }
  });
  socket.addEventListener("close", scheduleReconnect);
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  const delay = reconnectDelaysMs[reconnectAttempt];
  if (delay === undefined) {
    setConnection("offline");
    return;
  }
  reconnectAttempt += 1;
  setConnection("reconnecting");
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connectSimulationStream();
  }, delay);
}

function updateMutationControls(): void {
  const disabled = replayMode;
  pauseButton.disabled = disabled;
  resumeButton.disabled = disabled;
  resetButton.disabled = disabled;
  emergencyButton.disabled = disabled || runtimeCapabilities?.mode !== "bridge";
}

function replaceDisplayedWorld(config: WorldConfig, mode: "Authoritative" | "Replay"): void {
  if (worldVisuals) world.remove(worldVisuals.root);
  loadedWorld = config;
  worldVisuals = createWorldVisuals(config);
  world.add(worldVisuals.root);
  worldMode.textContent = `${config.scene_id} / ${mode}`;
  semanticWorldBadge.hidden = config.semantic_objects.length === 0;
}

async function exportReplay(): Promise<ReplayBundle> {
  const response = await fetch("/api/replays/current");
  if (response.status === 204) throw new Error("No completed Mission or Fault replay is available.");
  if (!response.ok) {
    const error = parseRuntimeApiError(await response.json().catch(() => null));
    throw new Error(error ? `${error.error_code}: ${error.error_message}` : `replay export failed with HTTP ${response.status}`);
  }
  const bundle = parseReplayBundle(await response.json());
  if (!bundle) throw new Error("Runtime returned an invalid Replay Bundle v1.");
  return bundle;
}

function enterReplay(bundle: ReplayBundle): void {
  replayMode = true;
  replayBundle = bundle;
  replayTimelineCount = -1;
  replayFrameRevision = -1;
  if (faultPollTimer !== null) {
    window.clearTimeout(faultPollTimer);
    faultPollTimer = null;
  }
  missionConsole.setReplayMode(true);
  faultConsole.setReplayMode(true);
  replaceDisplayedWorld(bundle.world, "Replay");
  updateMutationControls();
  renderRuntimeMode();
}

function projectReplayView(bundle: ReplayBundle, playback: ReplayPlaybackState): void {
  if (!replayMode || replayBundle?.replay_id !== bundle.replay_id) return;
  const projection = projectReplay(bundle, playback);
  if (projection.frame.revision !== replayFrameRevision) {
    replayFrameRevision = projection.frame.revision;
    displayFrame(projection.frame);
  }
  const timelineCount = projection.visibleEvents.length + projection.visibleEvidence.length;
  if (timelineCount === replayTimelineCount) return;
  replayTimelineCount = timelineCount;
  missionConsole.setSnapshot(projection.mission);
  faultConsole.setSnapshot(projection.fault);
  missionConsole.setFaultActive(false);
  faultConsole.setMissionActive(false);
}

async function loadLiveSnapshot(): Promise<void> {
  const response = await fetch("/api/simulation/snapshot");
  if (!response.ok) throw new Error(`simulation snapshot failed with HTTP ${response.status}`);
  const frame = parseSimulationFrame(await response.json());
  if (!frame) throw new Error("runtime returned an invalid SimulationFrame v1");
  const nextState = acceptSimulationFrame(liveViewState, frame);
  if (nextState !== liveViewState) liveViewState = nextState;
  displayFrame(frame, liveViewState.previousFrame ?? frame);
}

async function exitReplay(): Promise<void> {
  replayMode = false;
  replayBundle = null;
  replayTimelineCount = -1;
  replayFrameRevision = -1;
  missionConsole.setReplayMode(false);
  faultConsole.setReplayMode(false);
  renderRuntimeMode();
  updateMutationControls();
  if (liveWorld) replaceDisplayedWorld(liveWorld, "Authoritative");

  const results = await Promise.allSettled([
    loadWorld(),
    loadLiveSnapshot(),
    loadCurrentTask(),
    runtimeCapabilities?.fault_injection ? loadCurrentFault() : Promise.resolve(null),
  ]);
  const rejected = results.find((result) => result.status === "rejected");
  if (rejected?.status === "rejected") {
    const message = rejected.reason instanceof Error ? rejected.reason.message : "Live state synchronization failed.";
    missionConsole.setError(message);
    faultConsole.setError(message);
  }
}

async function runControl(button: HTMLButtonElement, action: "pause" | "resume" | "reset") {
  if (replayMode) return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/simulation/${action}`, { method: "POST" });
    if (!response.ok) throw new Error(`${action} failed with HTTP ${response.status}`);
    const frame = parseSimulationFrame(await response.json());
    if (!frame) throw new Error(`${action} returned an invalid simulation frame`);
    receiveLiveFrame(frame);
  } catch (error) {
    console.warn("simulation control failed", error);
  } finally {
    updateMutationControls();
  }
}

async function loadWorld(): Promise<void> {
  const response = await fetch("/api/simulation/world");
  if (!response.ok) throw new Error(`world config failed with HTTP ${response.status}`);
  const config = parseWorldConfig(await response.json());
  if (!config) throw new Error("runtime returned an invalid World Config v1");
  liveWorld = config;
  if (!replayMode) replaceDisplayedWorld(config, "Authoritative");
}

void loadWorld()
  .then(() => connectSimulationStream())
  .catch((error) => {
    console.warn("simulation world failed to load", error);
    worldMode.textContent = "World unavailable";
    setConnection("offline");
  });
void loadGo1Visual()
  .then((controller) => {
    robot = controller;
    if (controller.state === "ready") {
      world.add(controller.root);
      setAssetStatus("ready");
    } else {
      setAssetStatus("failed", controller.error);
    }
  })
  .catch((error) => {
    setAssetStatus("failed", error instanceof Error ? error.message : String(error));
  });
void loadRuntimeCapabilities().catch((error) => {
  missionConsole.setError(error instanceof Error ? error.message : "Runtime capabilities unavailable.");
  faultConsole.setError(error instanceof Error ? error.message : "Runtime capabilities unavailable.");
});

let resizeFrame: number | null = null;
let renderedWidth = 0;
let renderedHeight = 0;

function resize() {
  const width = scene.clientWidth;
  const height = Math.max(340, scene.clientHeight);
  if (width === renderedWidth && height === renderedHeight) return;
  renderedWidth = width;
  renderedHeight = height;
  if (width < 520) {
    camera.fov = 58;
    camera.position.set(3.2, 2.6, 3.8);
    controls.minDistance = 2.5;
    controls.maxDistance = 14;
  } else {
    camera.fov = 42;
    camera.position.set(3.8, 3, 4.4);
    controls.minDistance = 2.5;
    controls.maxDistance = 12;
  }
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

new ResizeObserver(() => {
  if (resizeFrame !== null) return;
  resizeFrame = window.requestAnimationFrame(() => {
    resizeFrame = null;
    resize();
  });
}).observe(scene);
resize();

function animate(time: number) {
  const latest = displayedFrame;
  if (latest) {
    const previous = displayedPreviousFrame ?? latest;
    const alpha = Math.min(1, Math.max(0, (time - displayedFrameReceivedAt) / 50));
    robot?.update(previous, latest, alpha);
    if (robot?.state === "ready") {
      controls.target.set(robot.root.position.x, 0.24, robot.root.position.z);
    }
  }
  controls.update();
  renderer.render(world, camera);
}
renderer.setAnimationLoop(animate);

pauseButton.addEventListener("click", () => void runControl(pauseButton, "pause"));
resumeButton.addEventListener("click", () => void runControl(resumeButton, "resume"));
resetButton.addEventListener("click", () => void runControl(resetButton, "reset"));
emergencyButton.addEventListener("click", () => void runEmergencyStop());
window.addEventListener("pagehide", () => {
  if (faultPollTimer !== null) window.clearTimeout(faultPollTimer);
  replayConsole.dispose();
  robot?.dispose();
}, { once: true });
