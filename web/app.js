/* DRIFT placeholder shell */

export const STATES = Object.freeze({
  NO_DATA: "no-data",
  ONE_EPOCH: "one-epoch",
  PARTIAL_COVERAGE: "partial-coverage",
  SATURATED: "saturated",
  LIVE_DEGRADED: "live-path-degraded",
  LIVE_UNAVAILABLE: "live-path-unavailable",
  LOADING: "loading",
  ERROR: "error",
});

const COPY = {
  [STATES.NO_DATA]: {
    title: "No snapshot published",
    message:
      "The service is running, but no snapshot exists yet. This is a placeholder page.",
  },
  [STATES.LOADING]: {
    title: "Loading",
    message: "Checking the published snapshot…",
  },
  [STATES.ERROR]: {
    title: "Service error",
    message: "The service could not be reached. Check /api/v1/health and retry.",
  },
};

export class StateView {
  constructor(root, { onRetry } = {}) {
    if (!root) throw new Error("StateView needs a root element");
    this.root = root;
    this.onRetry = onRetry;
    this.show(STATES.LOADING);
  }

  show(state, { title, message, retryable = false } = {}) {
    const copy = COPY[state] || COPY[STATES.ERROR];
    this.root.dataset.state = state;
    this.root.replaceChildren();

    const heading = document.createElement("h3");
    heading.textContent = title || copy.title;
    const body = document.createElement("p");
    body.textContent = message || copy.message;
    this.root.append(heading, body);

    if (retryable && typeof this.onRetry === "function") {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Retry";
      button.addEventListener("click", () => this.onRetry());
      this.root.append(button);
    }
  }
}

function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return "unknown age";
  if (seconds < 120) return `${Math.round(seconds)} s old`;
  if (seconds < 7200) return `${Math.round(seconds / 60)} min old`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h old`;
  return `${Math.round(seconds / 86400)} d old`;
}

async function loadHealth() {
  const response = await fetch("/api/v1/health", {
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) throw new Error(`health returned ${response.status}`);
  return response.json();
}

function renderHealth(health, view, badge) {
  if (!health.snapshot_id) {
    badge.dataset.state = "no-data";
    badge.textContent = "No snapshot published";
    view.show(STATES.NO_DATA);
    return;
  }
  badge.dataset.state = "ready";
  badge.textContent = `${health.snapshot_id} · ${formatAge(health.snapshot_age_s)} · ${health.plane_count} planes`;
  view.show(STATES.NO_DATA, {
    title: "Snapshot ready",
    message: `Snapshot ${health.snapshot_id} is published with ${health.plane_count} planes. The comparison renderer arrives in the next ticket.`,
  });
}

const view = new StateView(document.getElementById("state"));
const badge = document.getElementById("snapshot-badge");

async function refresh() {
  view.show(STATES.LOADING);
  try {
    renderHealth(await loadHealth(), view, badge);
  } catch (error) {
    badge.dataset.state = "error";
    badge.textContent = "Snapshot status unavailable";
    view.show(STATES.ERROR, { retryable: true });
    console.error("health check failed", error);
  }
}

void refresh();
