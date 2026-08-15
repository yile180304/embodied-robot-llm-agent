import {
  MAX_REPLAY_FILE_BYTES,
  advanceReplay,
  initialReplayPlayback,
  parseReplayFileText,
  seekReplay,
  setReplayPlaying,
  setReplaySpeed,
  stepReplay,
  type ReplayBundle,
  type ReplayPlaybackState,
  type ReplaySpeed,
} from "./replay.ts";

export type ReplayConsoleController = {
  setExportAvailable(available: boolean): void;
  dispose(): void;
};

function requiredElement<T extends Element>(root: ParentNode, selector: string): T {
  const element = root.querySelector<T>(selector);
  if (!element) throw new Error(`missing replay console element: ${selector}`);
  return element;
}

export function formatReplayTime(valueMs: number): string {
  const value = Math.max(0, Math.round(valueMs));
  const minutes = Math.floor(value / 60_000);
  const seconds = Math.floor(value % 60_000 / 1_000);
  const tenths = Math.floor(value % 1_000 / 100);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${tenths}`;
}

export function replayEvidenceCount(bundle: ReplayBundle): number {
  return bundle.run.kind === "mission"
    ? bundle.run.snapshot.events.length
    : bundle.run.snapshot.evidence.length;
}

export function modelTranscriptLabel(
  status: ReplayBundle["truthfulness"]["model_transcript"],
): string {
  if (status === "native_tool_calls_verified") return "Verified native Tool Calls";
  if (status === "not_verified") return "Not verified";
  return "Not applicable";
}

function downloadReplayBundle(bundle: ReplayBundle): void {
  const content = `${JSON.stringify(bundle, null, 2)}\n`;
  const url = URL.createObjectURL(new Blob([content], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${bundle.replay_id}.replay.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function createReplayConsole(
  root: HTMLElement,
  options: {
    onExport(): Promise<ReplayBundle>;
    onEnter(bundle: ReplayBundle): void;
    onProjection(bundle: ReplayBundle, state: ReplayPlaybackState): void;
    onExit(): Promise<void>;
  },
): ReplayConsoleController {
  root.className = "replay-console";
  root.innerHTML = `
    <div class="replay-heading">
      <div>
        <span class="section-label">REPLAY &amp; EVIDENCE</span>
        <strong>Local Bundle</strong>
      </div>
      <span class="replay-state" id="replay-state">LIVE</span>
    </div>
    <div class="replay-actions">
      <button class="replay-command" id="export-replay" type="button">
        <i data-lucide="download" aria-hidden="true"></i><span>Export current</span>
      </button>
      <button class="replay-command" id="import-replay" type="button">
        <i data-lucide="upload" aria-hidden="true"></i><span>Import JSON</span>
      </button>
      <input id="replay-file" type="file" accept="application/json,.json" hidden>
      <p class="replay-error" id="replay-error" role="status"></p>
    </div>
    <div class="replay-summary">
      <strong id="replay-title">No Replay Bundle loaded</strong>
      <p id="replay-detail">Export the latest completed Mission or Fault Run, or import a local JSON file.</p>
    </div>
    <div class="replay-playback" id="replay-playback" hidden>
      <div class="replay-transport">
        <button class="replay-icon-button" id="replay-step-back" type="button" aria-label="上一个回放关键帧" title="Previous recorded frame">
          <i data-lucide="skip-back" aria-hidden="true"></i>
        </button>
        <button class="replay-icon-button replay-toggle" id="replay-toggle" type="button" aria-label="播放回放" title="Play or pause replay" data-playing="false">
          <i class="replay-play-icon" data-lucide="play" aria-hidden="true"></i>
          <i class="replay-pause-icon" data-lucide="pause" aria-hidden="true"></i>
        </button>
        <button class="replay-icon-button" id="replay-step-forward" type="button" aria-label="下一个回放关键帧" title="Next recorded frame">
          <i data-lucide="skip-forward" aria-hidden="true"></i>
        </button>
        <span class="replay-time" id="replay-time">00:00.0 / 00:00.0</span>
      </div>
      <input class="replay-seek" id="replay-seek" type="range" min="0" max="1" step="10" value="0" aria-label="回放时间位置">
      <div class="replay-playback-footer">
        <div class="replay-speed" aria-label="回放速度">
          <button type="button" data-speed="0.5">0.5x</button>
          <button type="button" data-speed="1" data-active="true">1x</button>
          <button type="button" data-speed="2">2x</button>
        </div>
        <button class="replay-exit" id="exit-replay" type="button">
          <i data-lucide="log-out" aria-hidden="true"></i><span>Return live</span>
        </button>
      </div>
    </div>
    <div class="truthfulness-profile" id="truthfulness-profile" hidden>
      <div class="truthfulness-heading">
        <span class="section-label">TRUTHFULNESS PROFILE</span>
        <span id="replay-counts">0 frames</span>
      </div>
      <dl>
        <div><dt>Planner</dt><dd id="truth-planner">-</dd></div>
        <div><dt>Perception</dt><dd id="truth-perception">-</dd></div>
        <div><dt>MQTT</dt><dd id="truth-mqtt">-</dd></div>
        <div><dt>Model</dt><dd id="truth-model">-</dd></div>
        <div><dt>Hardware / EMQX</dt><dd id="truth-hardware">-</dd></div>
      </dl>
      <ul id="truth-claims"></ul>
    </div>
  `;

  const stateLabel = requiredElement<HTMLElement>(root, "#replay-state");
  const exportButton = requiredElement<HTMLButtonElement>(root, "#export-replay");
  const importButton = requiredElement<HTMLButtonElement>(root, "#import-replay");
  const fileInput = requiredElement<HTMLInputElement>(root, "#replay-file");
  const errorLabel = requiredElement<HTMLElement>(root, "#replay-error");
  const title = requiredElement<HTMLElement>(root, "#replay-title");
  const detail = requiredElement<HTMLElement>(root, "#replay-detail");
  const playbackPanel = requiredElement<HTMLElement>(root, "#replay-playback");
  const toggleButton = requiredElement<HTMLButtonElement>(root, "#replay-toggle");
  const stepBackButton = requiredElement<HTMLButtonElement>(root, "#replay-step-back");
  const stepForwardButton = requiredElement<HTMLButtonElement>(root, "#replay-step-forward");
  const seekInput = requiredElement<HTMLInputElement>(root, "#replay-seek");
  const timeLabel = requiredElement<HTMLElement>(root, "#replay-time");
  const exitButton = requiredElement<HTMLButtonElement>(root, "#exit-replay");
  const speedButtons = root.querySelectorAll<HTMLButtonElement>("[data-speed]");
  const truthfulness = requiredElement<HTMLElement>(root, "#truthfulness-profile");
  const counts = requiredElement<HTMLElement>(root, "#replay-counts");
  const truthPlanner = requiredElement<HTMLElement>(root, "#truth-planner");
  const truthPerception = requiredElement<HTMLElement>(root, "#truth-perception");
  const truthMqtt = requiredElement<HTMLElement>(root, "#truth-mqtt");
  const truthModel = requiredElement<HTMLElement>(root, "#truth-model");
  const truthHardware = requiredElement<HTMLElement>(root, "#truth-hardware");
  const truthClaims = requiredElement<HTMLUListElement>(root, "#truth-claims");

  let bundle: ReplayBundle | null = null;
  let playback: ReplayPlaybackState | null = null;
  let playbackFrame: number | null = null;
  let previousTick: number | null = null;
  let exportAvailable = false;
  let busy = false;

  function setError(message: string | null): void {
    errorLabel.textContent = message ?? "";
  }

  function renderPlayback(): void {
    if (!bundle || !playback) return;
    const playing = playback.status === "playing";
    toggleButton.dataset.playing = String(playing);
    toggleButton.setAttribute("aria-label", playing ? "暂停回放" : "播放回放");
    seekInput.max = String(Math.max(1, playback.duration_ms));
    seekInput.value = String(playback.cursor_ms);
    seekInput.disabled = playback.duration_ms === 0;
    timeLabel.textContent = `${formatReplayTime(playback.cursor_ms)} / ${formatReplayTime(playback.duration_ms)}`;
    speedButtons.forEach((button) => {
      button.dataset.active = String(Number(button.dataset.speed) === playback?.speed);
    });
  }

  function project(): void {
    if (!bundle || !playback) return;
    options.onProjection(bundle, playback);
    renderPlayback();
  }

  function stopClock(): void {
    if (playbackFrame !== null) cancelAnimationFrame(playbackFrame);
    playbackFrame = null;
    previousTick = null;
  }

  function tick(timestamp: number): void {
    playbackFrame = null;
    if (!bundle || !playback || playback.status !== "playing") {
      previousTick = null;
      return;
    }
    const elapsed = previousTick === null ? 0 : timestamp - previousTick;
    previousTick = timestamp;
    playback = advanceReplay(playback, elapsed);
    project();
    if (playback.status === "playing") playbackFrame = requestAnimationFrame(tick);
    else previousTick = null;
  }

  function startClock(): void {
    if (playbackFrame === null) playbackFrame = requestAnimationFrame(tick);
  }

  function showBundle(value: ReplayBundle): void {
    stopClock();
    bundle = value;
    playback = initialReplayPlayback(value);
    root.dataset.mode = "replay";
    root.dataset.truncated = String(value.frame_capture.truncated);
    stateLabel.textContent = "REPLAY";
    title.textContent = value.run.kind === "mission"
      ? value.run.snapshot.goal
      : `${value.run.snapshot.scenario} fault run`;
    detail.textContent = `${value.replay_id} · ${value.run.kind} · ${value.frame_capture.truncated ? "truncated capture" : "complete capture"}`;
    playbackPanel.hidden = false;
    truthfulness.hidden = false;
    counts.textContent = `${value.frame_capture.count} frames · ${replayEvidenceCount(value)} ${value.run.kind === "mission" ? "events" : "evidence"}`;
    truthPlanner.textContent = value.truthfulness.planner;
    truthPerception.textContent = value.truthfulness.perception_source;
    truthMqtt.textContent = value.truthfulness.mqtt_scope;
    truthModel.textContent = modelTranscriptLabel(value.truthfulness.model_transcript);
    truthHardware.textContent = `${value.truthfulness.hardware} / ${value.truthfulness.emqx}`;
    truthClaims.replaceChildren(...value.truthfulness.claims.map((claim) => {
      const item = document.createElement("li");
      item.textContent = claim;
      return item;
    }));
    setError(value.frame_capture.truncated ? "This capture is auditable but fails Acceptance Pack completeness." : null);
    options.onEnter(value);
    project();
  }

  function renderAvailability(): void {
    exportButton.disabled = busy || !exportAvailable;
    importButton.disabled = busy;
    exitButton.disabled = busy;
  }

  exportButton.addEventListener("click", () => {
    if (busy || !exportAvailable) return;
    busy = true;
    setError(null);
    renderAvailability();
    void options.onExport().then((value) => {
      downloadReplayBundle(value);
      detail.textContent = `Downloaded ${value.replay_id}.replay.json`;
    }).catch((error: unknown) => {
      setError(error instanceof Error ? error.message : "Replay export failed.");
    }).finally(() => {
      busy = false;
      renderAvailability();
    });
  });

  importButton.addEventListener("click", () => {
    if (!busy) fileInput.click();
  });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    busy = true;
    setError(null);
    renderAvailability();
    void (async () => {
      if (file.size > MAX_REPLAY_FILE_BYTES) throw new Error("Replay file exceeds the 5 MiB limit.");
      const parsed = parseReplayFileText(await file.text());
      if (!parsed) throw new Error("Replay file is invalid, unsupported, or exceeds strict limits.");
      showBundle(parsed);
    })().catch((error: unknown) => {
      setError(error instanceof Error ? error.message : "Replay import failed.");
    }).finally(() => {
      fileInput.value = "";
      busy = false;
      renderAvailability();
    });
  });

  toggleButton.addEventListener("click", () => {
    if (!playback) return;
    playback = setReplayPlaying(playback, playback.status !== "playing");
    project();
    if (playback.status === "playing") startClock();
    else stopClock();
  });

  stepBackButton.addEventListener("click", () => {
    if (!bundle || !playback) return;
    stopClock();
    playback = stepReplay(bundle, playback, -1);
    project();
  });

  stepForwardButton.addEventListener("click", () => {
    if (!bundle || !playback) return;
    stopClock();
    playback = stepReplay(bundle, playback, 1);
    project();
  });

  seekInput.addEventListener("input", () => {
    if (!playback) return;
    stopClock();
    playback = seekReplay(playback, Number(seekInput.value));
    project();
  });

  speedButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (!playback) return;
      playback = setReplaySpeed(playback, Number(button.dataset.speed) as ReplaySpeed);
      renderPlayback();
    });
  });

  exitButton.addEventListener("click", () => {
    if (busy || !bundle) return;
    busy = true;
    stopClock();
    renderAvailability();
    void options.onExit().then(() => {
      bundle = null;
      playback = null;
      root.dataset.mode = "live";
      root.dataset.truncated = "false";
      stateLabel.textContent = "LIVE";
      title.textContent = "No Replay Bundle loaded";
      detail.textContent = "Export the latest completed Mission or Fault Run, or import a local JSON file.";
      playbackPanel.hidden = true;
      truthfulness.hidden = true;
      setError(null);
    }).catch((error: unknown) => {
      setError(error instanceof Error ? error.message : "Live state could not be restored.");
    }).finally(() => {
      busy = false;
      renderAvailability();
    });
  });

  root.dataset.mode = "live";
  root.dataset.truncated = "false";
  renderAvailability();

  return {
    setExportAvailable(value) {
      exportAvailable = value;
      renderAvailability();
    },
    dispose() {
      stopClock();
    },
  };
}
