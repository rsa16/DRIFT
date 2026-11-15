<script lang="ts">
  import { onMount, tick } from "svelte";

  import MiniMap from "$lib/MiniMap.svelte";
  import StateView from "$lib/StateView.svelte";
  import {
    ApiError,
    fetchFrames,
    fetchHealth,
    fetchManifest,
    fetchPlane,
    fetchTarget,
    fetchTargets,
    type FramesResponse,
    type ManifestSummary,
    type PlaneMeta,
    type TargetDetail,
    type TargetSummary,
  } from "$lib/api";
  import {
    DEFAULT_FPS,
    MAX_FPS,
    MIN_FPS,
    BlinkController,
  } from "$lib/BlinkController";
  import {
    PlaneRenderer,
    RendererError,
    type RenderPreset,
  } from "$lib/PlaneRenderer";
  import {
    DEFAULT_FOV,
    parseShareState,
    writeShareState,
    type ViewState,
  } from "$lib/ShareState";
  import { decodeSpx1, Spx1Error, type DecodedPlane } from "$lib/spx1";

  const CITATION =
    "This publication makes use of data products from the Spectro-Photometer " +
    "for the History of the Universe, Epoch of Reionization and Ices Explorer " +
    "(SPHEREx), which is a joint project of the Jet Propulsion Laboratory and " +
    "the California Institute of Technology, and is funded by the National " +
    "Aeronautics and Space Administration.";

  const MODE_LABELS: Record<string, string> = {
    timeline: "Visit timeline",
    survey_compare: "Survey compare",
    spectrum: "Spectrum",
  };

  type StageMode = "timeline" | "compare";

  interface AppState {
    kind: string;
    title: string;
    message: string;
    retryable?: boolean;
  }

  interface PlaneEntry {
    meta: PlaneMeta;
    plane: DecodedPlane;
  }

  const DEFAULT_VISITS = 8;

  let manifest = $state<ManifestSummary | null>(null);
  let targets = $state<TargetSummary[]>([]);
  let detail = $state<TargetDetail | null>(null);
  let frames = $state<FramesResponse | null>(null);
  let decodedPlanes = $state<DecodedPlane[]>([]);
  let snapshotAge = $state<number | null>(null);
  let selectedId = $state<string>("");
  let appState = $state<AppState>({
    kind: "loading",
    title: "Loading",
    message: "Checking the published snapshot…",
  });
  let stage = $state<StageMode>("timeline");
  let difference = $state<boolean>(false);
  let preset = $state<RenderPreset>("linear");
  let fps = $state<number>(DEFAULT_FPS);
  let playing = $state<boolean>(true);
  let bandValue = $state<number>(0);
  let frameIndex = $state<number>(0);
  let referenceIndex = $state<number>(0);
  let altText = $state<string>("No frame loaded.");
  let statusMessage = $state<string>("");
  let view = $state<ViewState>({ ra: 0, dec: 0, fov: DEFAULT_FOV });
  let canvasA = $state<HTMLCanvasElement>();

  let rendererA: PlaneRenderer | null = null;
  let blink: BlinkController | null = null;
  let uploadedKey = "";

  const entries = $derived(
    frames && decodedPlanes.length === frames.planes.length
      ? frames.planes.map((meta, position) => ({
          meta,
          plane: decodedPlanes[position],
        }))
      : [],
  );
  const bands = $derived(detail?.timeline?.bands ?? []);
  const backend = $derived(
    stage === "timeline"
      ? (detail?.modes.timeline ?? { enabled: false, reason: "No timeline." })
      : (detail?.modes.survey_compare ?? {
          enabled: false,
          reason: "No survey compare.",
        }),
  );
  const modeRows = $derived(
    detail
      ? Object.entries(detail.modes).map(([key, value]) => ({
          key,
          label: MODE_LABELS[key] ?? key,
          enabled: value.enabled,
          reason: value.reason,
        }))
      : [],
  );
  const badgeState = $derived(
    manifest ? "ready" : appState.kind === "error" ? "error" : "loading",
  );
  const compareGroup = $derived(
    stage === "compare" && frames
      ? frames.planes
          .map((meta, position) => ({ meta, plane: decodedPlanes[position] }))
          .filter((entry) => entry.meta.band_index === bandValue)
      : [],
  );
  const strip = $derived(stage === "timeline" ? entries : compareGroup);
  const stripIndex = $derived(stage === "timeline" ? frameIndex : frameIndex);

  function separationDeg(ra1: number, dec1: number, ra2: number, dec2: number): number {
    const radians = Math.PI / 180;
    const cosine =
      Math.sin(dec1 * radians) * Math.sin(dec2 * radians) +
      Math.cos(dec1 * radians) *
        Math.cos(dec2 * radians) *
        Math.cos((ra1 - ra2) * radians);
    return Math.acos(Math.min(1, Math.max(-1, cosine))) / radians;
  }

  function formatAge(seconds: number | null): string {
    if (seconds === null || seconds === undefined) return "unknown age";
    if (seconds < 120) return `${Math.round(seconds)} s old`;
    if (seconds < 7200) return `${Math.round(seconds / 60)} min old`;
    if (seconds < 172800) return `${Math.round(seconds / 3600)} h old`;
    return `${Math.round(seconds / 86400)} d old`;
  }

  function formatRun(value: string | null | undefined): string {
    if (!value) return "unknown";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.valueOf())) return value;
    return `${parsed.toISOString().slice(0, 16).replace("T", " ")} UTC`;
  }

  function formatDate(value: string | null | undefined): string {
    if (!value) return "date unknown";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.valueOf())) return value;
    return `${parsed.toISOString().slice(0, 16).replace("T", " ")}Z`;
  }

  function shortDate(value: string | null | undefined): string {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.valueOf())) return value;
    return parsed.toISOString().slice(0, 10);
  }

  function formatMjd(value: number | null | undefined): string {
    return value === null || value === undefined ? "MJD —" : `MJD ${value.toFixed(3)}`;
  }

  function formatLambda(meta: PlaneMeta | undefined): string {
    if (!meta) return "—";
    return `${meta.lambda_lo_um.toFixed(2)}–${meta.lambda_hi_um.toFixed(2)} µm`;
  }

  function scaleText(): string {
    return detail ? `${detail.pixel_scale_arcsec.toFixed(3)}″/px` : "—";
  }

  function unitsText(): string {
    return detail?.units ?? frames?.units ?? "—";
  }

  function targetLabel(target: TargetSummary): string {
    return target.name ?? target.id;
  }

  function releaseText(meta: PlaneMeta | undefined): string {
    if (!meta) return "—";
    return meta.release.toUpperCase();
  }

  function visitLabel(meta: PlaneMeta | undefined): string {
    if (!meta) return "—";
    if (meta.visit_id) return meta.visit_id;
    return meta.epoch_id ?? "—";
  }

  function currentEntry(): PlaneEntry | undefined {
    if (stage === "timeline") return entries[frameIndex];
    return compareGroup[frameIndex];
  }

  function referenceEntry(): PlaneEntry | undefined {
    if (stage === "timeline") return entries[referenceIndex] ?? entries[0];
    return compareGroup[0];
  }

  function panelLabel(): string {
    const meta = currentEntry()?.meta;
    if (!meta) return "No image loaded";
    if (difference) {
      return (
        `Difference image: ${visitLabel(meta)} minus ${visitLabel(referenceEntry()?.meta)}, ` +
        `${formatLambda(meta)}, ${scaleText()}, ${unitsText()}`
      );
    }
    return (
      `Visit ${visitLabel(meta)} on ${formatDate(meta.date)}, ` +
      `${releaseText(meta)} ${meta.detector ?? ""}, ${formatLambda(meta)}, ` +
      `${scaleText()}, ${unitsText()}`
    );
  }

  function errorState(error: unknown): AppState {
    if (error instanceof Spx1Error) {
      return {
        kind: "error",
        title: "Plane rejected",
        message: `${error.message}. Nothing was drawn.`,
        retryable: true,
      };
    }
    if (error instanceof RendererError) {
      return {
        kind: "error",
        title: "Renderer unavailable",
        message: error.message,
        retryable: true,
      };
    }
    if (error instanceof ApiError) {
      return {
        kind: "error",
        title: "Service error",
        message: error.message,
        retryable: true,
      };
    }
    return {
      kind: "error",
      title: "Unexpected error",
      message: String(error),
      retryable: true,
    };
  }

  function teardownRenderers(): void {
    blink?.destroy();
    blink = null;
    rendererA?.dispose();
    rendererA = null;
    uploadedKey = "";
    frameIndex = 0;
    referenceIndex = 0;
  }

  function setupRenderers(): boolean {
    if (!canvasA) {
      appState = {
        kind: "error",
        title: "Renderer unavailable",
        message: "The image canvas was not mounted.",
      };
      return false;
    }
    try {
      rendererA = new PlaneRenderer(canvasA);
      return true;
    } catch (error) {
      appState = errorState(error);
      return false;
    }
  }

  function frameCount(): number {
    if (stage === "timeline") return entries.length;
    return compareGroup.length;
  }

  function initBlink(): void {
    blink?.destroy();
    blink = new BlinkController(Math.max(1, frameCount()), fps);
    blink.playing = playing && !difference;
    blink.onFrame((frame) => {
      if (difference) return;
      frameIndex = frame;
      drawFrame();
    });
    blink.start();
  }

  function drawFrame(force = false): void {
    if (!frames || !rendererA) return;
    if (stage === "timeline") {
      const entry = entries[frameIndex];
      if (!entry) return;
      const key = `timeline:${difference ? "diff" : frameIndex}:${bandValue}`;
      if (force || uploadedKey !== key) {
        if (difference) {
          const reference = referenceEntry();
          if (!reference) return;
          rendererA.setPlane(reference.plane, 0);
          rendererA.setPlane(entry.plane, 1);
        } else {
          rendererA.setPlane(entry.plane, 0);
        }
        uploadedKey = key;
      }
      rendererA.setWindow(frames.window);
      rendererA.setPreset(preset);
      rendererA.setMode(difference ? "difference" : "single");
      rendererA.draw();

      const meta = entry.meta;
      if (difference) {
        const reference = referenceEntry()?.meta;
        statusMessage =
          `Difference: ${visitLabel(meta)} − ${visitLabel(reference)} · ` +
          `${formatLambda(meta)} · one window for the whole stack`;
        altText =
          `Difference image: visit ${visitLabel(meta)} on ${formatDate(meta.date)} ` +
          `minus reference ${visitLabel(reference)} on ${formatDate(reference?.date)}. ` +
          `${formatLambda(meta)}, ${scaleText()}, ${unitsText()}. ` +
          "Blue means brighter in the current visit, amber means fainter, magenta marks missing pixels. Every frame in the stack shares one stretch window.";
      } else {
        statusMessage =
          `Visit ${frameIndex + 1} of ${entries.length} · ${formatDate(meta.date)} · ` +
          `${formatMjd(meta.mjd)} · ${releaseText(meta)} · ${formatLambda(meta)}`;
        altText =
          `Visit ${frameIndex + 1} of ${entries.length}: ${formatDate(meta.date)}, ` +
          `${formatMjd(meta.mjd)}, release ${releaseText(meta)}, detector ${meta.detector ?? "unknown"}, ` +
          `${formatLambda(meta)}, ${scaleText()}, ${unitsText()}. ` +
          "Frames play in date order at the selected speed and share one stretch window.";
      }
      return;
    }

    const group = compareGroup;
    if (group.length < 2) return;
    const entry = group[Math.min(frameIndex, group.length - 1)];
    const key = `compare:${difference ? "diff" : frameIndex}:${bandValue}`;
    if (force || uploadedKey !== key) {
      if (difference) {
        rendererA.setPlane(group[0].plane, 0);
        rendererA.setPlane(group[1].plane, 1);
      } else {
        rendererA.setPlane(entry.plane, 0);
      }
      uploadedKey = key;
    }
    rendererA.setWindow(frames.window);
    rendererA.setPreset(preset);
    rendererA.setMode(difference ? "difference" : "single");
    rendererA.draw();
    statusMessage = difference
      ? `Difference ${visitLabel(group[1].meta)} − ${visitLabel(group[0].meta)} · ${formatLambda(group[0].meta)}`
      : `Survey compare ${frameIndex + 1} of 2 · ${releaseText(entry.meta)} · ${formatLambda(entry.meta)}`;
    altText = difference
      ? `Difference image of the two survey epochs at ${formatLambda(group[0].meta)}, ${scaleText()}, ${unitsText()}.`
      : `Survey compare frame ${frameIndex + 1} of 2: ${releaseText(entry.meta)}, ${formatLambda(entry.meta)}, ${scaleText()}, ${unitsText()}.`;
  }

  function setFpsValue(value: number): void {
    fps = Math.min(MAX_FPS, Math.max(MIN_FPS, Math.round(value)));
    blink?.setFps(fps);
  }

  function setPresetValue(next: RenderPreset): void {
    preset = next;
    if (frames && rendererA) {
      drawFrame();
    }
  }

  function togglePlayback(): void {
    if (difference) return;
    playing = !playing;
    if (blink) blink.playing = playing;
  }

  function goTo(index: number): void {
    frameIndex = Math.max(0, Math.min(index, frameCount() - 1));
    playing = false;
    if (blink) {
      blink.playing = false;
      blink.goTo(frameIndex);
    }
    drawFrame(true);
    shareView();
  }

  function setDifference(next: boolean): void {
    difference = next;
    if (next) {
      playing = false;
      if (blink) blink.playing = false;
    }
    uploadedKey = "";
    drawFrame(true);
  }

  async function setBandValue(band: number): Promise<void> {
    if (!detail || stage !== "timeline") return;
    bandValue = band;
    await loadFrames(detail.id, band);
  }

  async function setStage(next: StageMode): Promise<void> {
    if (!detail || next === stage) return;
    stage = next;
    difference = false;
    await loadFrames(detail.id, stage === "timeline" ? bandValue : null);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.code !== "Space" || event.repeat) return;
    const target = event.target as HTMLElement | null;
    if (target?.closest("input, button, select, textarea, a, [contenteditable]")) {
      return;
    }
    event.preventDefault();
    togglePlayback();
  }

  function shareView(): void {
    view = {
      ...view,
      band: stage === "timeline" ? bandValue : undefined,
      visit: stage === "timeline" ? frameIndex : undefined,
    };
    writeShareState(view);
  }

  async function loadFrames(id: string, band: number | null): Promise<void> {
    teardownRenderers();
    frames = null;
    decodedPlanes = [];
    appState = {
      kind: "loading",
      title: "Loading",
      message: "Fetching the visit stack and its shared stretch window…",
    };
    try {
      const response =
        stage === "timeline"
          ? await fetchFrames(id, {
              mode: "timeline",
              band,
              visits: DEFAULT_VISITS,
            })
          : await fetchFrames(id, { mode: "epoch", bins: 8 });
      const buffers = await Promise.all(
        response.planes.map((plane) => fetchPlane(plane.href)),
      );
      const planes = buffers.map((buffer) => decodeSpx1(buffer));
      frames = response;
      decodedPlanes = planes;
      bandValue =
        stage === "timeline"
          ? (response.band ?? band ?? detail?.timeline?.default_band ?? 0)
          : 0;
      frameIndex =
        stage === "timeline"
          ? Math.min(view.visit ?? 0, response.planes.length - 1)
          : 0;
      referenceIndex = 0;
      await tick();
      if (!setupRenderers()) return;
      appState = { kind: "ready", title: "Ready", message: "" };
      uploadedKey = "";
      playing = view.visit === undefined;
      initBlink();
      blink?.goTo(frameIndex);
      drawFrame(true);
      shareView();
    } catch (error) {
      teardownRenderers();
      frames = null;
      decodedPlanes = [];
      appState = errorState(error);
    }
  }

  async function loadTarget(id: string, fov = view.fov): Promise<void> {
    teardownRenderers();
    frames = null;
    decodedPlanes = [];
    appState = {
      kind: "loading",
      title: "Loading",
      message: "Fetching visits and checking mode eligibility…",
    };
    try {
      const target = await fetchTarget(id);
      detail = target;
      selectedId = id;
      // Keep the shared band and frame: they are what a shared URL pins.
      const sharedBand = view.band;
      const sharedVisit = view.visit;
      view = {
        ra: target.ra ?? 0,
        dec: target.dec ?? 0,
        fov: fov,
        ...(sharedBand === undefined ? {} : { band: sharedBand }),
        ...(sharedVisit === undefined ? {} : { visit: sharedVisit }),
      };
      writeShareState(view);

      if (!target.modes.timeline?.enabled) {
        stage = "timeline";
        appState = {
          kind: "one-epoch",
          title: "Visit timeline unavailable",
          message:
            target.modes.timeline?.reason ??
            "This target does not have two visits at one band.",
        };
        return;
      }
      stage = "timeline";
      difference = false;
      bandValue = view.band ?? target.timeline?.default_band ?? 0;
      await loadFrames(id, bandValue);
    } catch (error) {
      teardownRenderers();
      frames = null;
      decodedPlanes = [];
      appState = errorState(error);
    }
  }

  async function loadInitial(): Promise<void> {
    teardownRenderers();
    frames = null;
    decodedPlanes = [];
    appState = {
      kind: "loading",
      title: "Loading",
      message: "Checking the published snapshot…",
    };
    try {
      const [manifestResponse, targetsResponse, health] = await Promise.all([
        fetchManifest(),
        fetchTargets(),
        fetchHealth(),
      ]);
      manifest = manifestResponse;
      targets = targetsResponse.targets;
      snapshotAge = health.snapshot_age_s;

      const shared = parseShareState(window.location.search);
      let chosen =
        targets.find((target) => target.modes.timeline?.enabled) ?? targets[0];

      if (shared) {
        view = shared;
        const match = targets.find(
          (target) =>
            target.ra !== null &&
            target.dec !== null &&
            separationDeg(target.ra, target.dec, shared.ra, shared.dec) <=
              Math.max(shared.fov / 2, 0.25),
        );
        if (!match) {
          selectedId = "";
          detail = null;
          appState = {
            kind: "no-data",
            title: "No data at this shared position",
            message:
              `The shared view is RA ${shared.ra.toFixed(5)}°, ` +
              `Dec ${shared.dec.toFixed(5)}°, FOV ${shared.fov.toFixed(2)}°. ` +
              "No snapshot target covers it, so nothing is drawn rather than " +
              "showing a different patch of sky.",
          };
          return;
        }
        chosen = match;
        if (shared.band) bandValue = shared.band;
      } else if (chosen) {
        view = {
          ra: chosen.ra ?? 0,
          dec: chosen.dec ?? 0,
          fov: DEFAULT_FOV,
        };
      }

      if (!chosen) {
        appState = {
          kind: "no-data",
          title: "No targets in this snapshot",
          message: "The snapshot published no targets.",
        };
        return;
      }
      await loadTarget(chosen.id, view.fov);
    } catch (error) {
      appState = errorState(error);
    }
  }

  function onTargetChange(): void {
    const target = targets.find((item) => item.id === selectedId);
    if (!target) return;
    // A band and a frame belong to one target's stack; never carry them across.
    view = { ra: view.ra, dec: view.dec, fov: view.fov };
    void loadTarget(target.id, view.fov);
  }

  function retry(): void {
    if (selectedId) {
      void loadTarget(selectedId, view.fov);
    } else {
      void loadInitial();
    }
  }

  onMount(() => {
    window.addEventListener("keydown", onKeydown);
    void loadInitial();
    return () => {
      window.removeEventListener("keydown", onKeydown);
      teardownRenderers();
    };
  });
</script>

<a class="skip-link" href="#main">Skip to content</a>

<header class="site-header">
  <div class="brand">
    <h1>DRIFT</h1>
    <p>Diffrential Infrared Frame Tracker</p>
  </div>
  <p
    class="badge"
    data-state={badgeState}
    data-fixture={manifest?.fixture ?? false}
    id="snapshot-badge"
  >
    {#if manifest}
      {#if manifest.fixture}
        <span class="fixture-flag">Fixture</span>
      {/if}
      <span>{manifest.snapshot_id}</span>
      <span>{formatAge(snapshotAge)}</span>
      <span>last run {formatRun(manifest.generated_at)}</span>
    {:else}
      Loading snapshot…
    {/if}
  </p>
</header>

<main id="main">
  <div class="layout">
    <section class="stage" aria-labelledby="stage-title">
      <div class="stage-header">
        <h2 id="stage-title">Matched-wavelength visit timeline</h2>
        {#if frames && stage === "timeline"}
          <span class="note">
            {frames.visits ?? entries.length} visits · band {frames.band} ·
            {frames.detector ?? "detector"} · one window
          </span>
        {:else if frames}
          <span class="note">
            {frames.bins ?? 0} bands × {frames.epochs?.length ?? 0} epochs · one
            window
          </span>
        {/if}
      </div>
      <p class="lede">
        Wavelengths are matched before any comparison, and every frame in a stack
        comes from one detector. Visits play in date order and share one
        ingest-computed stretch window, the same pixel scale, and the same units,
        so playback cannot flatter one visit. Motion below one pixel is measured,
        not seen.
      </p>

      {#if manifest?.fixture}
        <p class="fixture-warning" role="note">
          Fixture snapshot — synthetic pixels, not archive data. It exists to
          exercise the API and renderer, and the production publish path refuses
          it without an explicit flag.
        </p>
      {/if}

      {#if frames && strip.length}
        <div class="mode-tabs" role="tablist" aria-label="Image mode">
          <button
            type="button"
            role="tab"
            aria-selected={stage === "timeline"}
            disabled={!detail?.modes.timeline?.enabled}
            onclick={() => void setStage("timeline")}
          >
            Visit timeline
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={stage === "compare"}
            disabled={!detail?.modes.survey_compare?.enabled}
            onclick={() => void setStage("compare")}
          >
            Survey compare
          </button>
        </div>

        <div class="image-grid">
          <figure class="image-panel" data-panel="image">
            <canvas
              bind:this={canvasA}
              tabindex="0"
              aria-label={panelLabel()}
              aria-describedby="canvas-alt"
              aria-keyshortcuts="Space"
            ></canvas>
            <figcaption>
              <strong>
                {difference
                  ? `${visitLabel(currentEntry()?.meta)} − ${visitLabel(referenceEntry()?.meta)}`
                  : formatDate(currentEntry()?.meta?.date)}
              </strong>
              <span>{difference ? "difference" : formatMjd(currentEntry()?.meta?.mjd)}</span>
              <span>{releaseText(currentEntry()?.meta)}</span>
              <span>detector {currentEntry()?.meta?.detector ?? "—"}</span>
              <span>λ {formatLambda(currentEntry()?.meta)}</span>
              <span>{scaleText()}</span>
              <span>{unitsText()}</span>
              <span>
                {difference
                  ? `against ${formatDate(referenceEntry()?.meta?.date)}`
                  : `visit ${frameIndex + 1}/${strip.length}`}
              </span>
            </figcaption>
          </figure>
        </div>
        <p class="status" role="status" aria-live="polite">{statusMessage}</p>

        <section aria-label="Visit filmstrip">
          <ol class="filmstrip">
            {#each strip as entry, index (entry.meta.visit_id ?? entry.meta.index)}
              <li>
                <button
                  type="button"
                  aria-pressed={index === stripIndex}
                  aria-label={`Frame ${index + 1}: ${entry.meta.visit_id ?? entry.meta.epoch_id ?? "frame"}, ${formatDate(entry.meta.date)}, ${formatMjd(entry.meta.mjd ?? entry.meta.mjd_min)}, ${releaseText(entry.meta)}`}
                  onclick={() => goTo(index)}
                >
                  <span class="tick">{index + 1}</span>
                  <span class="date">
                    {entry.meta.date
                      ? shortDate(entry.meta.date)
                      : (entry.meta.epoch_id ?? "—")}
                  </span>
                  <span class="mjd">{formatMjd(entry.meta.mjd ?? entry.meta.mjd_min)}</span>
                  <span class="release">{releaseText(entry.meta)}</span>
                </button>
              </li>
            {/each}
          </ol>
          <p class="note">
            {#if stage === "timeline"}
              {#if frames.degraded}
                Degraded: {frames.frames_omitted?.length ?? 0}
                {(frames.frames_omitted?.length ?? 0) === 1 ? "visit" : "visits"}
                omitted, thinned evenly over MJD.
              {:else}
                {frames.visits ?? entries.length} of
                {frames.available_visits ?? entries.length} available visits shown.
              {/if}
            {:else}
              Two survey epochs, matched on measured wavelength.
            {/if}
          </p>
        </section>
      {:else}
        <StateView
          state={appState.kind}
          title={appState.title}
          message={appState.message}
          onRetry={appState.retryable ? retry : undefined}
        />
      {/if}

      <p id="canvas-alt" class="sr-only" aria-live="polite">{altText}</p>

      <section class="provenance" aria-label="Provenance">
        <p>
          Visit
          <strong>{currentEntry()?.meta?.visit_id ?? "—"}</strong>
          {formatDate(currentEntry()?.meta?.date)}
        </p>
        <p>{formatMjd(currentEntry()?.meta?.mjd)}</p>
        <p>
          Release <strong>{releaseText(currentEntry()?.meta)}</strong> · detector
          <strong>{currentEntry()?.meta?.detector ?? "—"}</strong>
        </p>
        <p>λ <strong>{formatLambda(currentEntry()?.meta)}</strong> matched</p>
        <p>Pixel scale <strong>{scaleText()}</strong></p>
        <p>Units <strong>{unitsText()}</strong></p>
        <p>
          Window sha
          <strong>{frames ? frames.window.sha256.slice(0, 12) : "—"}</strong>
        </p>
      </section>

      <section class="controls" aria-label="Playback and stretch controls">
        <div>
          <label for="fps">Playback speed <output>{fps} fps</output></label>
          <input
            id="fps"
            type="range"
            min={MIN_FPS}
            max={MAX_FPS}
            step="1"
            value={fps}
            aria-valuetext={`${fps} frames per second`}
            oninput={(event) => setFpsValue(Number(event.currentTarget.value))}
            disabled={!strip.length || difference}
          />
        </div>
        <div>
          <button
            type="button"
            onclick={togglePlayback}
            aria-pressed={playing}
            disabled={!strip.length || difference}
          >
            {difference ? "Paused for difference" : playing ? "Pause" : "Play"}
          </button>
          <button
            type="button"
            aria-pressed={difference}
            onclick={() => setDifference(!difference)}
            disabled={!strip.length || frameIndex === referenceIndex}
            title={frameIndex === referenceIndex
              ? "This frame is the difference reference; choose another visit."
              : "Compare this frame with the reference visit."}
          >
            Difference
          </button>
          <p class="note">
            Space toggles playback when focus is not in a control. Difference
            compares the current visit with a reference visit from the same stack;
            it is disabled while the current frame is that reference.
          </p>
        </div>
        <div>
          <label for="band">
            Wavelength band
            <output>{frames?.band ?? bandValue}</output>
          </label>
          <select
            id="band"
            value={String(bandValue)}
            onchange={(event) => void setBandValue(Number(event.currentTarget.value))}
            disabled={!strip.length || stage !== "timeline" || bands.length < 2}
          >
            {#each bands as band (band)}
              <option value={String(band)}>Band {band}</option>
            {/each}
          </select>
        </div>
        <div>
          <label for="preset">Stretch preset</label>
          <select
            id="preset"
            value={preset}
            onchange={(event) =>
              setPresetValue(event.currentTarget.value as RenderPreset)}
            disabled={!strip.length}
          >
            <option value="linear">Linear</option>
            <option value="asinh">Asinh</option>
            <option value="log">Log</option>
          </select>
        </div>
        <p class="note">
          Linear, asinh and log all derive from this band's single window (lo, hi,
          s); changing preset never recomputes the window.
        </p>
      </section>
    </section>

    <aside class="sidebar" aria-label="Target and mode details">
      <div class="card">
        <h2>Target</h2>
        <label class="sr-only" for="target-select">Target</label>
        <select
          id="target-select"
          bind:value={selectedId}
          onchange={onTargetChange}
        >
          {#each targets as target (target.id)}
            <option value={target.id}>{targetLabel(target)}</option>
          {/each}
        </select>

        {#if detail}
          <dl>
            <dt>Name</dt>
            <dd>{targetLabel(detail)}</dd>
            <dt>Class</dt>
            <dd>{detail.kind ?? "unknown"}</dd>
            <dt>RA</dt>
            <dd>{detail.ra === null ? "—" : `${detail.ra.toFixed(5)}°`}</dd>
            <dt>Dec</dt>
            <dd>{detail.dec === null ? "—" : `${detail.dec.toFixed(5)}°`}</dd>
            <dt>Default band</dt>
            <dd>
              {detail.timeline?.default_band ?? "—"} · detector
              {detail.timeline?.default_detector ?? "—"}
            </dd>
            <dt>Visits</dt>
            <dd>
              {detail.timeline?.visits ?? 0} at the default band · span
              {detail.timeline?.span_days === null ||
              detail.timeline?.span_days === undefined
                ? "—"
                : `${detail.timeline.span_days.toFixed(2)} d`}
            </dd>
            <dt>Epochs</dt>
            <dd>
              {detail.epochs
                .map(
                  (epoch) =>
                    `${epoch.release.toUpperCase()} (MJD ${
                      epoch.mjd_min === null ? "—" : epoch.mjd_min.toFixed(2)
                    })`,
                )
                .join(" · ")}
            </dd>
            <dt>Pixel scale</dt>
            <dd>{detail.pixel_scale_arcsec.toFixed(3)}″/px</dd>
            <dt>Units</dt>
            <dd>{detail.units}</dd>
            <dt>Field of view</dt>
            <dd>{view.fov.toFixed(2)}°</dd>
          </dl>
          <MiniMap
            ra={detail.ra ?? view.ra}
            dec={detail.dec ?? view.dec}
            fov={view.fov}
            label={`Sky position RA ${(detail.ra ?? view.ra).toFixed(
              3,
            )} degrees, Dec ${(detail.dec ?? view.dec).toFixed(
              3,
            )} degrees, field of view ${view.fov.toFixed(2)} degrees`}
          />
          {#if detail.caveats?.length}
            <ul class="caveats">
              {#each detail.caveats as caveat}
                <li>{caveat}</li>
              {/each}
            </ul>
          {/if}
          <h3>Modes</h3>
          <ul class="mode-list">
            {#each modeRows as row (row.key)}
              <li data-enabled={row.enabled}>
                <strong>{row.label}</strong>
                <span>
                  {row.enabled ? "Available" : `Not available: ${row.reason}`}
                </span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="note">No target loaded for this view.</p>
        {/if}
      </div>

      <div class="card">
        <h2>Shareable view</h2>
        <dl>
          <dt>RA</dt>
          <dd>{view.ra.toFixed(5)}°</dd>
          <dt>Dec</dt>
          <dd>{view.dec.toFixed(5)}°</dd>
          <dt>FOV</dt>
          <dd>{view.fov.toFixed(2)}°</dd>
          <dt>Band</dt>
          <dd>{view.band ?? "—"}</dd>
          <dt>Visit</dt>
          <dd>{view.visit === undefined ? "—" : view.visit + 1}</dd>
        </dl>
        <p class="note">
          The URL query <code>?ra=&amp;dec=&amp;fov=&amp;band=&amp;visit=</code>
          restores this view, down to the frame, on reload and can be shared.
        </p>
      </div>
    </aside>
  </div>
</main>

<footer class="site-footer">
  <div>
    <p class="citation">{manifest?.citation ?? CITATION}</p>
    <p>
      SPHEREx data via IRSA. Gaia DR3, AllWISE/NEOWISE, SkyBoT/IMCCE, Astropy and
      reproject are cited in the submission. Code: AGPL-3.0.
    </p>
  </div>
  <p><a href="/api/v1/health">Service health</a></p>
</footer>
