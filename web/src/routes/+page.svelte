<script lang="ts">
  import { onMount, tick } from "svelte";

  import Dock from "$lib/components/Dock.svelte";
  import Header from "$lib/components/Header.svelte";
  import Rail from "$lib/components/Rail.svelte";
  import Stage from "$lib/components/Stage.svelte";
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
  import {
    bandTicks,
    canvasAltText,
    captionFacts,
    frameFacts,
    visitLabel,
    type StackFacts,
  } from "$lib/format";
  import { buildHistogram } from "$lib/histogram";

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
  let tone = $state<RenderPreset>("asinh");
  let fps = $state<number>(DEFAULT_FPS);
  let playing = $state<boolean>(true);
  let bandValue = $state<number>(0);
  let bandIndex = $state<number>(0);
  let frameIndex = $state<number>(0);
  let referenceIndex = $state<number>(0);
  let statusMessage = $state<string>("");
  let view = $state<ViewState>({ ra: 0, dec: 0, fov: DEFAULT_FOV });
  let canvasA = $state<HTMLCanvasElement>();
  let railOpen = $state<boolean>(true);

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
  const compareGroup = $derived(
    stage === "compare" && frames
      ? frames.planes
          .map((meta, position) => ({ meta, plane: decodedPlanes[position] }))
          .filter((entry) => entry.meta.band_index === bandIndex)
      : [],
  );
  const steps = $derived(stage === "timeline" ? entries : compareGroup);
  const compareBands = $derived(
    stage === "compare" && frames ? bandTicks(frames.planes) : [],
  );
  const current = $derived<PlaneEntry | null>(steps[frameIndex] ?? null);
  const reference = $derived<PlaneEntry | null>(
    steps[referenceIndex] ?? steps[0] ?? null,
  );
  const stackMeta = $derived<PlaneMeta | undefined>(current?.meta);
  const canDifference = $derived(
    !!current && steps.length > 1 && frameIndex !== referenceIndex,
  );
  const ready = $derived(appState.kind === "ready" && frames !== null);

  const currentFacts = $derived(frameFacts(current?.meta));
  const referenceFacts = $derived(frameFacts(reference?.meta));
  const stackFacts = $derived<StackFacts | null>(
    frames && stackMeta
      ? {
          detector: stackMeta.detector ?? frames.detector ?? null,
          lambdaLoUm: stackMeta.lambda_lo_um,
          lambdaHiUm: stackMeta.lambda_hi_um,
          pixelScaleArcsec: frames.pixel_scale_arcsec,
          units: frames.units,
          windowSha: frames.window.sha256,
        }
      : null,
  );
  const caption = $derived(currentFacts ? captionFacts(currentFacts) : []);
  const notes = $derived([
    ...(detail?.caveats ?? []),
    ...(frames?.refused_frames ?? []).map((id) => `Refused frame ${id}.`),
  ]);
  const altText = $derived.by(() => {
    if (!currentFacts || !stackFacts) return "No frame loaded.";
    if (difference && referenceFacts) {
      return (
        `Difference image: visit ${visitLabel(currentFacts)} against reference ` +
        `${visitLabel(referenceFacts)}. ${canvasAltText(currentFacts, stackFacts)} ` +
        "Blue means brighter in the current visit, amber fainter, magenta missing pixels."
      );
    }
    return canvasAltText(currentFacts, stackFacts);
  });
  const canvasLabel = $derived(difference ? "Difference image" : "Sky image");
  const histogram = $derived(
    buildHistogram(current?.plane ?? null, frames?.window ?? null),
  );

  function separationDeg(ra1: number, dec1: number, ra2: number, dec2: number): number {
    const radians = Math.PI / 180;
    const cosine =
      Math.sin(dec1 * radians) * Math.sin(dec2 * radians) +
      Math.cos(dec1 * radians) *
        Math.cos(dec2 * radians) *
        Math.cos((ra1 - ra2) * radians);
    return Math.acos(Math.min(1, Math.max(-1, cosine))) / radians;
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

  function initBlink(): void {
    blink?.destroy();
    blink = new BlinkController(Math.max(1, steps.length), fps);
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
    const entry = steps[frameIndex];
    if (!entry) return;
    const referenceEntry = steps[referenceIndex] ?? steps[0];
    const stackKey = stage === "timeline" ? bandValue : bandIndex;
    const key = `${stage}:${difference ? "diff" : frameIndex}:${stackKey}:${frames.window.sha256}`;
    if (force || uploadedKey !== key) {
      if (difference && referenceEntry) {
        rendererA.setPlane(referenceEntry.plane, 0);
        rendererA.setPlane(entry.plane, 1);
      } else {
        rendererA.setPlane(entry.plane, 0);
      }
      uploadedKey = key;
    }
    rendererA.setWindow(frames.window);
    rendererA.setPreset(tone);
    rendererA.setMode(difference ? "difference" : "single");
    rendererA.draw();
    statusMessage = difference
      ? "Difference image on."
      : stage === "compare"
        ? `Survey compare step ${frameIndex + 1} of ${steps.length}.`
        : `Visit ${frameIndex + 1} of ${steps.length}.`;
  }

  function redrawForResize(): void {
    rendererA?.draw();
  }

  function setFpsValue(value: number): void {
    fps = Math.min(MAX_FPS, Math.max(MIN_FPS, Math.round(value)));
    blink?.setFps(fps);
  }

  function setToneValue(next: RenderPreset): void {
    tone = next;
    drawFrame();
  }

  function togglePlayback(): void {
    if (difference || !steps.length) return;
    playing = !playing;
    if (blink) blink.playing = playing;
  }

  function goTo(index: number): void {
    if (!steps.length) return;
    frameIndex = Math.max(0, Math.min(index, steps.length - 1));
    if (difference && frameIndex === referenceIndex) difference = false;
    playing = false;
    if (blink) {
      blink.playing = false;
      blink.goTo(frameIndex);
    }
    drawFrame(true);
    shareView();
  }

  function setReference(index: number): void {
    if (!steps.length) return;
    referenceIndex = Math.max(0, Math.min(index, steps.length - 1));
    if (difference && frameIndex === referenceIndex) difference = false;
    uploadedKey = "";
    drawFrame(true);
  }

  function setCompareBand(index: number): void {
    if (stage !== "compare" || bandIndex === index) return;
    bandIndex = index;
    // The pair changes together, so the first epoch is the new current step.
    frameIndex = 0;
    if (referenceIndex === 0) difference = false;
    uploadedKey = "";
    drawFrame(true);
  }

  function step(delta: number): void {
    goTo(frameIndex + delta);
  }

  function setDifference(next: boolean): void {
    if (next && !canDifference) return;
    difference = next;
    if (next) {
      playing = false;
      if (blink) blink.playing = false;
    }
    uploadedKey = "";
    drawFrame(true);
  }

  async function setBandValue(band: number): Promise<void> {
    if (!detail || stage !== "timeline" || band === bandValue) return;
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
    if (event.repeat || event.defaultPrevented) return;
    const target = event.target as HTMLElement | null;
    if (target?.closest("input, select, textarea, [contenteditable]")) return;
    if (event.code === "Space") {
      if (target?.closest("button, a")) return;
      event.preventDefault();
      togglePlayback();
      return;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      step(event.key === "ArrowLeft" ? -1 : 1);
    }
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
      if (stage === "timeline") {
        bandValue =
          response.band ?? band ?? detail?.timeline?.default_band ?? 0;
      } else {
        // Compare bins are 0-based indices; the timeline band is preserved.
        bandIndex = Math.max(0, response.planes[0]?.band_index ?? 0);
      }
      frameIndex =
        stage === "timeline"
          ? Math.min(view.visit ?? 0, response.planes.length - 1)
          : 0;
      referenceIndex = 0;
      difference = false;
      appState = { kind: "ready", title: "Ready", message: "" };
      await tick();
      if (!setupRenderers()) return;
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

  function onTargetChange(id: string): void {
    const target = targets.find((item) => item.id === id);
    if (!target) return;

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

<a
  class="sr-only rounded-control bg-text px-2 py-1 text-bg focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50"
  href="#main">Skip to content</a
>

<div
  class="flex min-h-0 flex-col overflow-hidden bg-bg text-text"
  style="height:100vh; height:100dvh;"
>
  <Header
    {manifest}
    {snapshotAge}
    {targets}
    {selectedId}
    {railOpen}
    onTarget={onTargetChange}
    onToggleRail={() => (railOpen = !railOpen)}
  />

  <main id="main" class="flex min-h-0 flex-1">
    <div class="flex min-h-0 min-w-0 flex-1 flex-col">
      <Stage
        bind:canvas={canvasA}
        {ready}
        stateKind={appState.kind}
        stateTitle={appState.title}
        stateMessage={appState.message}
        retry={appState.retryable ? retry : undefined}
        frame={currentFacts}
        {caption}
        {altText}
        {statusMessage}
        label={canvasLabel}
        pixelScaleArcsec={frames?.pixel_scale_arcsec ?? 0}
        planePx={frames?.planes[0]?.width ?? 512}
        {difference}
        {notes}
        onResize={redrawForResize}
      />

      <Dock
        steps={steps}
        index={frameIndex}
        {playing}
        {difference}
        {canDifference}
        {tone}
        {bands}
        band={stage === "timeline"
          ? bandValue
          : (compareGroup[0]?.meta.band ?? bandIndex)}
        {compareBands}
        {bandIndex}
        {referenceIndex}
        {fps}
        minFps={MIN_FPS}
        maxFps={MAX_FPS}
        degraded={frames?.degraded ?? false}
        omitted={frames?.frames_omitted ?? []}
        shown={frames?.visits ?? steps.length}
        available={frames?.available_visits ?? steps.length}
        compare={stage === "compare"}
        disabled={!steps.length || !ready}
        onGoTo={goTo}
        onStep={step}
        onTogglePlay={togglePlayback}
        onDifference={() => setDifference(!difference)}
        onReference={setReference}
        onTone={setToneValue}
        onBand={(band) => void setBandValue(band)}
        onCompareBand={setCompareBand}
        onFps={setFpsValue}
      />
    </div>

    <Rail
      {detail}
      {frames}
      stack={stackFacts}
      modes={modeRows}
      {view}
      citation={manifest?.citation ?? CITATION}
      band={frames
        ? stage === "timeline"
          ? bandValue
          : (compareGroup[0]?.meta.band ?? bandIndex)
        : null}
      {stage}
      {tone}
      {histogram}
      window={frames?.window ?? null}
      sourceUrl={current?.meta.source_url ?? null}
      open={railOpen}
      onStage={(next) => void setStage(next)}
    />
  </main>
</div>
