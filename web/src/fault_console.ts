import type { RuntimeCapabilities } from "./mission.ts";
import type { FaultEvidence, FaultRunSnapshot, FaultScenario } from "./fault.ts";

export type FaultEvidenceRow = {
  key: string;
  tone: "prepare" | "transport" | "observation" | "timeout" | "service" | "final";
  label: string;
  meta: string;
  detail: string;
};

export type FaultConsoleController = {
  setCapabilities(capabilities: RuntimeCapabilities): void;
  setSnapshot(snapshot: FaultRunSnapshot | null): void;
  setMissionActive(active: boolean): void;
  setReplayMode(active: boolean): void;
  setError(message: string | null): void;
  setSubmitting(submitting: boolean): void;
};

const SCENARIOS: Array<{ value: FaultScenario; label: string }> = [
  { value: "device_disconnect", label: "Device disconnect" },
  { value: "response_timeout", label: "Response timeout" },
  { value: "duplicate_delivery", label: "Duplicate delivery" },
  { value: "out_of_order", label: "Out of order" },
];

function requiredElement<T extends Element>(root: ParentNode, selector: string): T {
  const element = root.querySelector<T>(selector);
  if (!element) throw new Error(`missing fault console element: ${selector}`);
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

export function buildFaultEvidenceRows(evidence: readonly FaultEvidence[]): FaultEvidenceRow[] {
  return evidence.map((item) => {
    const correlation = item.task_id && item.seq ? `${item.task_id} #${item.seq}` : item.stage;
    if (item.stage === "prepare") {
      return {
        key: item.event_id,
        tone: "prepare",
        label: "Prepare",
        meta: item.stage,
        detail: text(item.payload.scenario),
      };
    }
    if (item.stage === "published") {
      const command = record(item.payload.command);
      return {
        key: item.event_id,
        tone: "transport",
        label: "MQTT Publish",
        meta: correlation,
        detail: `${text(command?.tool)} · QoS ${String(item.payload.qos)}`,
      };
    }
    if (item.stage === "observation") {
      const observation = record(item.payload.observation);
      const errorCode = text(observation?.error_code);
      return {
        key: item.event_id,
        tone: "observation",
        label: item.payload.replayed === true ? "Observation / Replay" : "Observation",
        meta: correlation,
        detail: `${text(observation?.status)}${errorCode ? ` · ${errorCode}` : ""}`,
      };
    }
    if (item.stage === "timeout") {
      return {
        key: item.event_id,
        tone: "timeout",
        label: "Caller Timeout",
        meta: correlation,
        detail: `${String(item.payload.timeout_ms)} ms · ${text(item.payload.error_message)}`,
      };
    }
    if (item.stage === "service_disconnected" || item.stage === "service_restored") {
      const restored = item.stage === "service_restored";
      return {
        key: item.event_id,
        tone: "service",
        label: restored ? "Service Restored" : "Service Disconnected",
        meta: text(item.payload.device_id),
        detail: restored ? "connected=true" : "connected=false · clean disconnect",
      };
    }
    return {
      key: item.event_id,
      tone: "final",
      label: "Fault Final",
      meta: text(item.payload.result),
      detail: text(item.payload.summary),
    };
  });
}

export function createFaultConsole(
  root: HTMLElement,
  options: {
    onRun(scenario: FaultScenario): Promise<void>;
  },
): FaultConsoleController {
  root.innerHTML = `
    <div class="fault-heading">
      <div>
        <span class="section-label">FAULT CONSOLE</span>
        <strong>MQTT Injection</strong>
      </div>
      <span class="fault-state" id="fault-state">UNAVAILABLE</span>
    </div>
    <form class="fault-form" id="fault-form">
      <label class="field-label" for="fault-scenario">Scenario</label>
      <div class="fault-controls">
        <select id="fault-scenario">
          ${SCENARIOS.map((scenario) => `<option value="${scenario.value}">${scenario.label}</option>`).join("")}
        </select>
        <button class="fault-run-button" id="run-fault" type="submit">
          <i data-lucide="flask-conical" aria-hidden="true"></i><span>Run fault</span>
        </button>
      </div>
      <p class="fault-error" id="fault-error" role="status"></p>
    </form>
    <div class="fault-summary">
      <strong id="fault-title">No fault run</strong>
      <p id="fault-detail">Bridge fault injection is unavailable.</p>
    </div>
    <div class="fault-evidence-heading">
      <span class="section-label">EVIDENCE</span>
      <span id="fault-evidence-count">0 items</span>
    </div>
    <ol class="fault-evidence" id="fault-evidence"></ol>
  `;

  const form = requiredElement<HTMLFormElement>(root, "#fault-form");
  const scenario = requiredElement<HTMLSelectElement>(root, "#fault-scenario");
  const runButton = requiredElement<HTMLButtonElement>(root, "#run-fault");
  const state = requiredElement<HTMLElement>(root, "#fault-state");
  const error = requiredElement<HTMLElement>(root, "#fault-error");
  const title = requiredElement<HTMLElement>(root, "#fault-title");
  const detail = requiredElement<HTMLElement>(root, "#fault-detail");
  const evidenceCount = requiredElement<HTMLElement>(root, "#fault-evidence-count");
  const evidenceList = requiredElement<HTMLOListElement>(root, "#fault-evidence");

  let capabilities: RuntimeCapabilities | null = null;
  let snapshot: FaultRunSnapshot | null = null;
  let missionActive = false;
  let submitting = false;
  let replayMode = false;
  let errorMessage: string | null = null;

  function renderEvidence(): void {
    const rows = buildFaultEvidenceRows(snapshot?.evidence ?? []);
    evidenceCount.textContent = `${rows.length} item${rows.length === 1 ? "" : "s"}`;
    evidenceList.replaceChildren();
    if (rows.length === 0) {
      const empty = document.createElement("li");
      empty.className = "fault-evidence-empty";
      empty.textContent = "No fault evidence yet.";
      evidenceList.appendChild(empty);
      return;
    }
    for (const row of rows) {
      const item = document.createElement("li");
      item.className = "fault-evidence-item";
      item.dataset.tone = row.tone;
      const header = document.createElement("div");
      const label = document.createElement("strong");
      const meta = document.createElement("span");
      const rowDetail = document.createElement("p");
      header.className = "fault-evidence-item-header";
      label.textContent = row.label;
      meta.textContent = row.meta;
      rowDetail.textContent = row.detail;
      header.append(label, meta);
      item.append(header, rowDetail);
      evidenceList.appendChild(item);
    }
  }

  function renderStatus(): void {
    const available = capabilities?.mode === "bridge" && capabilities.fault_injection && !replayMode;
    const running = snapshot?.status === "accepted" || snapshot?.status === "running";
    state.textContent = replayMode
      ? "REPLAY"
      : running
      ? "RUNNING"
      : snapshot?.status === "finished"
        ? String(snapshot.result ?? "FINISHED").toUpperCase()
        : available ? "READY" : "UNAVAILABLE";
    state.dataset.state = replayMode ? "replay" : running ? "running" : snapshot?.result ?? "idle";
    scenario.disabled = submitting || running || !available || missionActive;
    runButton.disabled = submitting || running || !available || missionActive;
    error.textContent = errorMessage ?? "";
    if (snapshot) {
      title.textContent = SCENARIOS.find((item) => item.value === snapshot?.scenario)?.label ?? snapshot.scenario;
      detail.textContent = replayMode
        ? `recorded ${snapshot.run_id}`
        : snapshot.status === "finished"
        ? `${snapshot.result ?? "finished"} · ${snapshot.summary ?? "Fault run ended"}`
        : `${snapshot.status} · ${snapshot.run_id}`;
    } else if (missionActive) {
      title.textContent = "Mission owns the runtime";
      detail.textContent = "Fault commands become available when the active Mission finishes.";
    } else if (replayMode) {
      title.textContent = "No Fault Evidence in this Mission Bundle";
      detail.textContent = "Mission RuntimeEvents remain in the Agent timeline above.";
    } else if (available) {
      title.textContent = "Ready for a fault run";
      detail.textContent = `${capabilities?.device_id ?? "device"} · fixed scenarios`;
    } else {
      title.textContent = "Fault injection unavailable";
      detail.textContent = "Preview mode keeps simulation controls available.";
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (replayMode) return;
    const selected = scenario.value as FaultScenario;
    if (!SCENARIOS.some((item) => item.value === selected)) return;
    submitting = true;
    errorMessage = null;
    renderStatus();
    void options.onRun(selected).catch((runError: unknown) => {
      errorMessage = runError instanceof Error ? runError.message : "Fault run failed.";
    }).finally(() => {
      submitting = false;
      renderStatus();
    });
  });

  renderEvidence();
  renderStatus();

  return {
    setCapabilities(value) {
      capabilities = value;
      renderStatus();
    },
    setSnapshot(value) {
      snapshot = value;
      renderEvidence();
      renderStatus();
    },
    setMissionActive(value) {
      missionActive = value;
      renderStatus();
    },
    setReplayMode(value) {
      replayMode = value;
      errorMessage = null;
      if (value) {
        submitting = false;
        missionActive = false;
      }
      renderStatus();
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
