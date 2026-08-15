import {
  mergeRuntimeEvents,
  type MissionTaskRequest,
  type MissionTaskSnapshot,
  type RuntimeCapabilities,
  type RuntimeEvent,
} from "./mission.ts";

type MissionPreset = MissionTaskRequest & { label: string };

export type TimelineRow = {
  key: string;
  tone: "goal" | "planning" | "action" | "observation" | "replanning" | "rejected" | "final";
  label: string;
  meta: string;
  detail: string;
};

export type MissionConsoleController = {
  setCapabilities(capabilities: RuntimeCapabilities): void;
  setSnapshot(snapshot: MissionTaskSnapshot | null): void;
  setFaultActive(active: boolean): void;
  setReplayMode(active: boolean): void;
  acceptEvent(event: RuntimeEvent): void;
  setError(message: string | null): void;
  setSubmitting(submitting: boolean): void;
};

export function modelProviderStatus(configured: boolean): string {
  return configured
    ? "Configured OpenAI-compatible provider"
    : "Model unavailable · configure provider and restart runtime";
}

const PRESETS: MissionPreset[] = [
  {
    label: "Obstacle detour",
    goal: "前进 2 米，如果遇到障碍就从更宽的一侧绕开",
    planner: "fake",
    max_steps: 8,
  },
  {
    label: "Straight move",
    goal: "前进 1 米",
    planner: "fake",
    max_steps: 4,
  },
  {
    label: "Read state",
    goal: "读取当前机器人状态",
    planner: "fake",
    max_steps: 4,
  },
  {
    label: "Semantic search",
    goal: "找到红色瓶子并前往蓝色目标区",
    planner: "fake",
    max_steps: 16,
  },
];

function requiredElement<T extends Element>(root: ParentNode, selector: string): T {
  const element = root.querySelector<T>(selector);
  if (!element) throw new Error(`missing mission console element: ${selector}`);
  return element;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function eventDetail(event: RuntimeEvent): string {
  const payload = event.payload;
  if (event.phase === "user_goal") return text(payload.goal);
  if (event.phase === "planning") {
    const trigger = payload.blocked_triggered === true ? " · blocked feedback" : "";
    return `Step ${String(payload.step_count)} / ${String(payload.max_steps)}${trigger}`;
  }
  if (event.phase === "tool_call") {
    return `${text(payload.planner_message)} · ${JSON.stringify(payload.arguments)}`;
  }
  if (event.phase === "published") {
    return `MQTT QoS ${String(payload.qos)} · ${text(payload.topic)}`;
  }
  if (event.phase === "observation") {
    const observation = record(payload.observation);
    const status = text(observation?.status) || "unknown";
    const code = text(observation?.error_code);
    return `${status}${code ? ` · ${code}` : ""} · ${String(payload.latency_ms)} ms`;
  }
  if (event.phase === "replanning") return text(payload.reason);
  if (event.phase === "safety_rejected") {
    const observation = record(payload.observation);
    return `${text(observation?.error_code) || "rejected"} · published=false`;
  }
  return `${text(payload.final_status)} · ${text(payload.final_message)}`;
}

function standaloneRow(event: RuntimeEvent): TimelineRow {
  const definitions = {
    user_goal: ["goal", "Goal"],
    planning: ["planning", "Planning"],
    tool_call: ["action", "Action"],
    published: ["action", "MQTT Publish"],
    observation: ["observation", "Observation"],
    replanning: ["replanning", "Replanning"],
    safety_rejected: ["rejected", "Safety Rejection"],
    final: ["final", "Final"],
  } as const;
  const [tone, label] = definitions[event.phase];
  return {
    key: event.event_id,
    tone,
    label,
    meta: event.seq > 0 ? `${event.tool ?? "agent"} #${event.seq}` : event.task_id,
    detail: eventDetail(event),
  };
}

export function buildTimelineRows(events: readonly RuntimeEvent[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    const next = events[index + 1];
    if (
      event.phase === "tool_call"
      && next?.phase === "published"
      && next.task_id === event.task_id
      && next.seq === event.seq
    ) {
      rows.push({
        key: `${event.event_id}:${next.event_id}`,
        tone: "action",
        label: "Action / MQTT Publish",
        meta: `${event.tool ?? "tool"} #${event.seq}`,
        detail: `${eventDetail(event)}\n${eventDetail(next)}`,
      });
      index += 1;
      continue;
    }
    rows.push(standaloneRow(event));
  }
  return rows;
}

export function createMissionConsole(
  root: HTMLElement,
  options: {
    onSubmit(request: MissionTaskRequest): Promise<void>;
    onCancel(): Promise<void>;
  },
): MissionConsoleController {
  root.innerHTML = `
    <div class="mission-heading">
      <div>
        <span class="section-label">MISSION</span>
        <strong>Agent Task</strong>
      </div>
      <span class="mission-state" id="mission-state">UNAVAILABLE</span>
    </div>
    <div class="preset-list" aria-label="预设任务">
      ${PRESETS.map((preset, index) => `
        <button type="button" class="preset-button" data-preset="${index}">${preset.label}</button>
      `).join("")}
    </div>
    <form class="mission-form" id="mission-form">
      <label class="field-label" for="mission-goal">Goal</label>
      <textarea id="mission-goal" rows="3" maxlength="512"></textarea>
      <div class="mission-options">
        <fieldset class="planner-control">
          <legend>Planner</legend>
          <label><input type="radio" name="planner" value="fake" checked><span>Fake</span></label>
          <label><input type="radio" name="planner" value="model"><span>Model</span></label>
          <p class="model-provider-state" id="model-provider-state">Model status unavailable</p>
        </fieldset>
        <label class="step-control" for="mission-steps">
          <span>Max steps</span>
          <input id="mission-steps" type="number" min="1" max="16" step="1" value="8">
        </label>
      </div>
      <div class="mission-actions">
        <button class="run-button" id="run-mission" type="submit">
          <i data-lucide="send" aria-hidden="true"></i><span>Run mission</span>
        </button>
        <button class="cancel-button" id="cancel-mission" type="button" hidden>
          <i data-lucide="x" aria-hidden="true"></i><span>Cancel mission</span>
        </button>
      </div>
      <p class="mission-error" id="mission-error" role="status"></p>
    </form>
    <div class="task-summary">
      <span class="section-label">CURRENT TASK</span>
      <strong id="task-title">No mission submitted</strong>
      <p id="task-detail">Bridge mode is required to run Agent tasks.</p>
    </div>
    <div class="timeline-heading">
      <span class="section-label">AGENT TIMELINE</span>
      <span id="event-count">0 events</span>
    </div>
    <ol class="mission-timeline" id="mission-timeline"></ol>
  `;

  const form = requiredElement<HTMLFormElement>(root, "#mission-form");
  const goal = requiredElement<HTMLTextAreaElement>(root, "#mission-goal");
  const steps = requiredElement<HTMLInputElement>(root, "#mission-steps");
  const runButton = requiredElement<HTMLButtonElement>(root, "#run-mission");
  const cancelButton = requiredElement<HTMLButtonElement>(root, "#cancel-mission");
  const modelPlanner = requiredElement<HTMLInputElement>(root, 'input[name="planner"][value="model"]');
  const modelProviderState = requiredElement<HTMLElement>(root, "#model-provider-state");
  const plannerInputs = root.querySelectorAll<HTMLInputElement>('input[name="planner"]');
  const presetButtons = root.querySelectorAll<HTMLButtonElement>("[data-preset]");
  const missionState = requiredElement<HTMLElement>(root, "#mission-state");
  const missionError = requiredElement<HTMLElement>(root, "#mission-error");
  const taskTitle = requiredElement<HTMLElement>(root, "#task-title");
  const taskDetail = requiredElement<HTMLElement>(root, "#task-detail");
  const eventCount = requiredElement<HTMLElement>(root, "#event-count");
  const timeline = requiredElement<HTMLOListElement>(root, "#mission-timeline");

  let capabilities: RuntimeCapabilities | null = null;
  let snapshot: MissionTaskSnapshot | null = null;
  let events: RuntimeEvent[] = [];
  let submitting = false;
  let cancelling = false;
  let faultActive = false;
  let replayMode = false;
  let errorMessage: string | null = null;

  function renderTimeline(): void {
    const rows = buildTimelineRows(events);
    eventCount.textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
    timeline.replaceChildren();
    if (rows.length === 0) {
      const empty = document.createElement("li");
      empty.className = "timeline-empty";
      empty.textContent = "No mission events yet.";
      timeline.appendChild(empty);
      return;
    }
    for (const row of rows) {
      const item = document.createElement("li");
      item.className = "timeline-item";
      item.dataset.tone = row.tone;
      const header = document.createElement("div");
      header.className = "timeline-item-header";
      const label = document.createElement("strong");
      label.textContent = row.label;
      const meta = document.createElement("span");
      meta.textContent = row.meta;
      const detail = document.createElement("p");
      detail.textContent = row.detail;
      header.append(label, meta);
      item.append(header, detail);
      timeline.appendChild(item);
    }
  }

  function renderStatus(): void {
    const available = capabilities?.mode === "bridge" && !replayMode;
    const running = snapshot?.status === "accepted" || snapshot?.status === "running";
    missionState.textContent = replayMode
      ? "REPLAY"
      : running
      ? "RUNNING"
      : snapshot?.status === "finished"
        ? String(snapshot.final_status ?? "FINISHED").toUpperCase()
        : available ? "READY" : "UNAVAILABLE";
    missionState.dataset.state = replayMode ? "replay" : running ? "running" : snapshot?.status ?? "idle";
    runButton.hidden = !replayMode && running;
    cancelButton.hidden = replayMode || !running;
    runButton.disabled = submitting || !available || running || faultActive;
    cancelButton.disabled = cancelling || !available || !running;
    goal.disabled = replayMode;
    steps.disabled = replayMode;
    presetButtons.forEach((button) => { button.disabled = replayMode; });
    plannerInputs.forEach((input) => {
      input.disabled = replayMode || (input === modelPlanner && !capabilities?.model_configured);
    });
    modelProviderState.textContent = capabilities
      ? modelProviderStatus(capabilities.model_configured)
      : "Model status unavailable";
    modelProviderState.dataset.configured = capabilities?.model_configured === true ? "true" : "false";
    missionError.textContent = errorMessage ?? "";
    if (snapshot) {
      taskTitle.textContent = snapshot.goal;
      taskDetail.textContent = replayMode
        ? `${snapshot.planner} planner · recorded ${snapshot.task_id}`
        : snapshot.status === "finished"
        ? `${snapshot.final_status ?? "finished"} · ${snapshot.final_message ?? "Mission ended"}`
        : `${snapshot.planner} planner · ${snapshot.status} · ${snapshot.task_id}`;
    } else if (faultActive) {
      taskTitle.textContent = "Fault Run owns the runtime";
      taskDetail.textContent = "Mission commands become available when the Fault Run finishes.";
    } else if (replayMode) {
      taskTitle.textContent = "No Mission timeline in this Fault Bundle";
      taskDetail.textContent = "Fault Evidence remains separate from RuntimeEvent playback.";
    } else if (available) {
      taskTitle.textContent = "Ready for a mission";
      taskDetail.textContent = `${capabilities?.device_id ?? "device"} · ${capabilities?.mqtt_endpoint ?? "MQTT"}`;
    } else {
      taskTitle.textContent = "Mission execution unavailable";
      taskDetail.textContent = "Preview mode keeps the deterministic simulation controls available.";
    }
  }

  presetButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (replayMode) return;
      const index = Number(button.dataset.preset);
      const preset = PRESETS[index];
      if (!preset) return;
      goal.value = preset.goal;
      steps.value = String(preset.max_steps);
      const planner = requiredElement<HTMLInputElement>(root, `input[name="planner"][value="${preset.planner}"]`);
      planner.checked = true;
    });
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (replayMode) return;
    const planner = requiredElement<HTMLInputElement>(root, 'input[name="planner"]:checked');
    const maxSteps = Number(steps.value);
    const normalizedGoal = goal.value.trim();
    if (!normalizedGoal || !Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > 16) {
      errorMessage = "Enter a goal and choose 1 to 16 steps.";
      renderStatus();
      return;
    }
    submitting = true;
    errorMessage = null;
    renderStatus();
    void options.onSubmit({
      goal: normalizedGoal,
      planner: planner.value === "model" ? "model" : "fake",
      max_steps: maxSteps,
    }).catch((error: unknown) => {
      errorMessage = error instanceof Error ? error.message : "Mission submission failed.";
    }).finally(() => {
      submitting = false;
      renderStatus();
    });
  });

  cancelButton.addEventListener("click", () => {
    if (replayMode) return;
    cancelling = true;
    errorMessage = null;
    renderStatus();
    void options.onCancel().catch((error: unknown) => {
      errorMessage = error instanceof Error ? error.message : "Mission cancellation failed.";
    }).finally(() => {
      cancelling = false;
      renderStatus();
    });
  });

  goal.value = PRESETS[0].goal;
  renderTimeline();
  renderStatus();

  return {
    setCapabilities(value) {
      capabilities = value;
      renderStatus();
    },
    setSnapshot(value) {
      snapshot = value;
      events = value
        ? replayMode ? [...value.events] : mergeRuntimeEvents(value.events, events)
        : [];
      renderTimeline();
      renderStatus();
    },
    setFaultActive(value) {
      faultActive = value;
      renderStatus();
    },
    setReplayMode(value) {
      replayMode = value;
      errorMessage = null;
      if (value) {
        submitting = false;
        cancelling = false;
        faultActive = false;
      }
      renderStatus();
    },
    acceptEvent(event) {
      if (
        event.phase === "user_goal"
        && (snapshot?.task_id !== event.task_id || events.some((current) => current.task_id !== event.task_id))
      ) {
        events = [];
        snapshot = null;
      }
      events = mergeRuntimeEvents(events, [event]);
      renderTimeline();
      if (event.phase === "final") renderStatus();
    },
    setError(message) {
      errorMessage = message;
      renderStatus();
    },
    setSubmitting(value) {
      submitting = value;
      renderStatus();
    },
  };
}
