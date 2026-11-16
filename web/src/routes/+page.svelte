<script lang="ts">
  import { onMount, tick } from "svelte";

  import DegradedBanner from "$lib/components/DegradedBanner.svelte";
  import Dock from "$lib/components/Dock.svelte";
  import Header from "$lib/components/Header.svelte";
  import LiveDialog from "$lib/components/LiveDialog.svelte";
  import Rail from "$lib/components/Rail.svelte";
  import Stage from "$lib/components/Stage.svelte";
  import {
    ApiError,
    fetchFrames,
    fetchHealth,
    fetchManifest,
    fetchPlane,
    fetchResolve,
    fetchTarget,
    fetchTargets,
    type FramesResponse,
    type ManifestSummary,
    type PlaneMeta,
    type ReferenceVisit,
    type ResolveResponse,
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
    formatDate,
    formatMjd,
    frameFacts,
    releaseLabel,
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
  let rgb = $state<boolean>(false);
  let diffPlane = $state<DecodedPlane | null>(null);
  let diffMeta = $state<PlaneMeta | null>(null);
  let diffReference = $state<ReferenceVisit | null>(null);
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
  let liveOpen = $state<boolean>(false);
  let liveBusy = $state<boolean>(false);
  let liveNotice = $state<string>("");
  let loadGeneration = 0;
  let liveQuery = $state<{
    ra: number;
    dec: number;
    fov: number;
    bins: number;
  } | null>(null);

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
    !rgb && !!current && steps.length > 1 && frameIndex !== referenceIndex,
  );
  const canRgb = $derived(
    stage === "timeline" && !!frames && steps.length >= 3 && !difference,
  );
  const ready = $derived(appState.kind === "ready" && frames !== null);
  const liveEnabled = $derived(manifest?.live_enabled === true);
  const liveActive = $derived(selectedId === "live");

  const currentFacts = $derived(frameFacts(current?.meta));
  const referenceFacts = $derived(frameFacts(reference?.meta));
  const diffBandpass = $derived(
    diffMeta?.bandpass_delta_um === undefined
      ? null
      : {
          deltaUm: diffMeta.bandpass_delta_um,
          fraction: diffMeta.bandpass_fraction ?? 0,
        },
  );
  const stackFacts = $derived<StackFacts | null>(
    frames && stackMeta
      ? {
          detector: stackMeta.detector ?? frames.detector ?? null,
          lambdaLoUm: stackMeta.lambda_lo_um,
          lambdaHiUm: stackMeta.lambda_hi_um,
          pixelScaleArcsec:
            frames.planes[0]?.pixel_scale_arcsec ??
            frames.pixel_scale_arcsec,
          units: frames.units,
          windowSha: frames.window.sha256,
        }
      : null,
  );
  const caption = $derived.by(() => {
    if (rgb && frames?.mode === "rgb") {
      return [
        "Temporal RGB",
        frames.planes
          .map(
            (plane) =>
              `${(plane.channel ?? "?").toUpperCase()} ` +
              `${formatDate(plane.date).slice(0, 10)} ` +
              `${releaseLabel(plane.release)}`,
          )
          .join(" · "),
      ];
    }
    return currentFacts ? captionFacts(currentFacts) : [];
  });
  const notes = $derived([
    ...(detail?.caveats ?? []),
    ...(frames?.refused_frames ?? []).map((id) => `Refused frame ${id}.`),
  ]);
  const altText = $derived.by(() => {
    if (!currentFacts || !stackFacts) return "No frame loaded.";
    if (rgb && frames?.mode === "rgb") {
      return (
        `Temporal RGB composite of ${frames.planes.length} visits. ` +
        frames.planes
          .map(
            (plane) =>
              `${(plane.channel ?? "?").toUpperCase()}: visit ${plane.visit_id ?? "—"}, ` +
              `${formatDate(plane.date)}, ${formatMjd(plane.mjd)}, ` +
              `release ${releaseLabel(plane.release)}.`,
          )
          .join(" ") +
        ` ${canvasAltText(currentFacts, stackFacts)}`
      );
    }
    if (difference && referenceFacts) {
      const reference = frames?.reference;
      return (
        `Difference image: visit ${visitLabel(currentFacts)} against reference ` +
        `${reference?.visit_id ?? visitLabel(referenceFacts)}. ` +
        (diffMeta?.cross_release
          ? "This pair spans processing releases; a pipeline difference may be present. "
          : "") +
        `${canvasAltText(currentFacts, stackFacts)} ` +
        "Blue means brighter in the current visit, amber fainter, magenta missing pixels."
      );
    }
    return canvasAltText(currentFacts, stackFacts);
  });
  const canvasLabel = $derived(
    difference ? "Difference image" : rgb ? "Temporal RGB composite" : "Sky image",
  );
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
    diffPlane = null;
    diffMeta = null;
    diffReference = null;
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
    blink.playing = playing && !difference && !rgb;
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
    if (difference && stage === "timeline" && !diffPlane) return;
    const referenceEntry = steps[referenceIndex] ?? steps[0];
    const stackKey = stage === "timeline" ? bandValue : bandIndex;
    const key = `${stage}:${rgb ? "rgb" : difference ? `diff:${diffMeta?.sha256 ?? ""}:${referenceIndex}` : frameIndex}:${stackKey}:${frames.window.sha256}`;
    if (force || uploadedKey !== key) {
      if (difference && stage === "timeline" && diffPlane) {
        rendererA.setPlane(diffPlane, 0);
      } else if (difference && referenceEntry) {
        rendererA.setPlane(referenceEntry.plane, 0);
        rendererA.setPlane(entry.plane, 1);
      } else if (rgb && entries.length >= 3) {
        entries.slice(0, 3).forEach((item, slot) => {
          rendererA?.setPlane(item.plane, slot as 0 | 1 | 2);
        });
      } else {
        rendererA.setPlane(entry.plane, 0);
      }
      uploadedKey = key;
    }
    rendererA.setWindow(frames.window);
    rendererA.setDifferenceSigma(
      difference && stage === "timeline"
        ? (diffMeta?.sigma ??
            Math.sqrt(diffMeta?.sigma_squared ?? 0))
        : 0,
    );
    rendererA.setPreset(tone);
    rendererA.setMode(
      difference
        ? stage === "timeline"
          ? "difference-plane"
          : "difference"
        : rgb
          ? "rgb"
          : "single",
    );
    rendererA.draw();
    statusMessage = difference
      ? `Difference image: ${currentFacts ? visitLabel(currentFacts) : "visit"} against ${frames.reference?.visit_id ?? "the reference visit"}.`
      : rgb
        ? "Temporal RGB composite of three visits on one window."
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
    if (difference || rgb || !steps.length) return;
    playing = !playing;
    if (blink) blink.playing = playing;
  }

  function sameReleaseReference(index: number): number {
    const release = steps[index]?.meta.release;
    const currentLambda = steps[index]?.meta.lambda_um;
    let best = -1;
    let bestDelta = Number.POSITIVE_INFINITY;
    steps.forEach((step, position) => {
      if (position === index || step.meta.release !== release) return;
      if (best < 0) best = position;
      const lambda = step.meta.lambda_um;
      if (currentLambda === undefined || lambda === undefined) return;
      const delta = Math.abs(currentLambda - lambda);
      if (delta < bestDelta) {
        best = position;
        bestDelta = delta;
      }
    });
    return best >= 0 ? best : referenceIndex;
  }

  function goTo(index: number): void {
    if (!steps.length || rgb) return;
    frameIndex = Math.max(0, Math.min(index, steps.length - 1));
    if (
      difference &&
      steps[referenceIndex]?.meta.release !== steps[frameIndex]?.meta.release
    ) {
      referenceIndex = sameReleaseReference(frameIndex);
    }
    if (difference && frameIndex === referenceIndex) {
      difference = false;
      diffPlane = null;
      diffMeta = null;
    diffReference = null;
    }
    playing = false;
    if (blink) {
      blink.playing = false;
      blink.goTo(frameIndex);
    }
    if (difference) {
      void loadDifference();
    } else {
      drawFrame(true);
    }
    shareView();
  }

  function setReference(index: number): void {
    if (!steps.length) return;
    referenceIndex = Math.max(0, Math.min(index, steps.length - 1));
    if (difference && frameIndex === referenceIndex) {
      difference = false;
      diffPlane = null;
      diffMeta = null;
    diffReference = null;
    }
    uploadedKey = "";
    if (difference) {
      void loadDifference();
    } else {
      drawFrame(true);
    }
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

  async function loadDifference(): Promise<void> {
    if (!detail || !frames) return;
    if (stage !== "timeline") {
      drawFrame(true);
      return;
    }
    statusMessage = "Computing the difference on the stack's one window…";
    try {
      const response = await fetchFrames(detail.id, {
        mode: "diff",
        band: bandValue,
        // Steps are thinned; visit identity survives, the step index does not.
        reference: steps[referenceIndex]?.meta.visit_id ?? referenceIndex,
        visit: current?.meta.visit_id ?? frameIndex,
      });
      const plane = response.planes[0];
      if (!plane) throw new ApiError(409, "No difference plane was returned.");
      diffPlane = decodeSpx1(await fetchPlane(plane.href));
      diffMeta = plane;
      diffReference = response.reference ?? null;
      uploadedKey = "";
      drawFrame(true);
    } catch (error) {
      difference = false;
      diffPlane = null;
      diffMeta = null;
    diffReference = null;
      statusMessage =
        error instanceof ApiError ? error.message : "Difference failed.";
      drawFrame(true);
    }
  }

  async function setDifference(next: boolean): Promise<void> {
    if (next && !canDifference) return;
    difference = next;
    if (next) {
      playing = false;
      if (blink) blink.playing = false;
      if (
        steps[referenceIndex]?.meta.release !== steps[frameIndex]?.meta.release
      ) {
        // A pipeline zero-point difference is not a sky change; default the
        // reference into the current visit's release.
        referenceIndex = sameReleaseReference(frameIndex);
        uploadedKey = "";
      }
      await loadDifference();
    } else {
      diffPlane = null;
      diffMeta = null;
    diffReference = null;
      uploadedKey = "";
      drawFrame(true);
    }
  }

  async function setRgb(next: boolean): Promise<void> {
    if (!detail || stage !== "timeline" || rgb === next) return;
    rgb = next;
    difference = false;
    diffPlane = null;
    diffMeta = null;
    diffReference = null;
    await loadFrames(detail.id, bandValue);
  }

  async function setBandValue(band: number): Promise<void> {
    if (!detail || stage !== "timeline" || band === bandValue) return;
    bandValue = band;
    await loadFrames(detail.id, band);
  }

  async function setStage(next: StageMode): Promise<void> {
    if (!detail || next === stage) return;
    if (detail.id === "live" && next === "compare") return;
    stage = next;
    difference = false;
    rgb = false;
    diffPlane = null;
    diffMeta = null;
    diffReference = null;
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

  function liveDetail(
    response: ResolveResponse,
    ra: number,
    dec: number,
    fov: number,
  ): TargetDetail {
    const liveFrames = response.frames as FramesResponse;
    const releases = new Set(
      (liveFrames.visits_meta ?? []).map((visit) => visit.release),
    );
    const band = liveFrames.band ?? 0;
    return {
      id: "live",
      name: `Live RA ${ra.toFixed(4)}° Dec ${dec.toFixed(4)}°`,
      kind: "live archive position",
      ra,
      dec,
      labels: ["live"],
      fixture: false,
      snapshot_id: liveFrames.snapshot_id,
      epoch_count: releases.size,
      visit_count: liveFrames.visits ?? liveFrames.planes.length,
      frame_count: liveFrames.planes.length,
      modes: {
        ...response.modes,
        survey_compare: {
          enabled: false,
          reason:
            "Live positions serve a visit timeline; survey compare is available " +
            "for pinned targets.",
        },
      },
      timeline: {
        default_band: band,
        default_detector: liveFrames.detector ?? null,
        visits: liveFrames.visits ?? 0,
        span_days: liveFrames.span_days ?? 0,
        bands: [band],
      },
      caveats: [
        "Live archive path: these planes were fetched and reprojected for this " +
          "position now; they are not part of the pinned snapshot.",
        `Field of view ${fov.toFixed(3)}°.`,
      ],
      pixel_scale_arcsec: liveFrames.pixel_scale_arcsec,
      units: liveFrames.units,
      window: liveFrames.window,
      epochs: [],
    };
  }

  async function loadLive(
    ra: number,
    dec: number,
    fov: number,
    bins = DEFAULT_VISITS,
  ): Promise<void> {
    const generation = ++loadGeneration;
    teardownRenderers();
    frames = null;
    decodedPlanes = [];
    detail = null;
    selectedId = "live";
    liveQuery = { ra, dec, fov, bins };
    liveBusy = true;
    liveNotice = "";
    appState = {
      kind: "loading",
      title: "Resolving live position",
      message:
        `Searching the live archive near RA ${ra.toFixed(4)}°, ` +
        `Dec ${dec.toFixed(4)}°…`,
    };
    try {
      const response = await fetchResolve(ra, dec, fov, bins);
      if (generation !== loadGeneration) return;
      if (response.pinned) {
        liveBusy = false;
        liveOpen = false;
        await loadTarget(response.target_id, fov);
        return;
      }
      if (response.state !== "ready" || !response.frames) {
        liveBusy = false;
        liveOpen = false;
        liveNotice = response.reason;
        appState = {
          kind: response.degraded ? "degraded" : "unavailable",
          title: response.degraded
            ? "Live timeline degraded"
            : "Live position unavailable",
          message:
            response.reason ||
            "The live archive could not produce a timeline for this position.",
          retryable: true,
        };
        return;
      }
      const buffers = await Promise.all(
        response.frames.planes.map((plane) => fetchPlane(plane.href)),
      );
      if (generation !== loadGeneration) return;
      const planes = buffers.map((buffer) => decodeSpx1(buffer));
      const liveFrames = response.frames as FramesResponse;
      frames = liveFrames;
      decodedPlanes = planes;
      detail = liveDetail(response, ra, dec, fov);
      stage = "timeline";
      bandValue = liveFrames.band ?? 0;
      bandIndex = 0;
      frameIndex = 0;
      referenceIndex = 0;
      difference = false;
      view = { ra, dec, fov };
      writeShareState(view);
      appState = { kind: "ready", title: "Ready", message: "" };
      liveBusy = false;
      liveOpen = false;
      await tick();
      if (generation !== loadGeneration) return;
      if (!setupRenderers()) return;
      uploadedKey = "";
      playing = true;
      initBlink();
      blink?.goTo(0);
      drawFrame(true);
      shareView();
    } catch (error) {
      if (generation !== loadGeneration) return;
      liveBusy = false;
      liveNotice = error instanceof Error ? error.message : String(error);
      teardownRenderers();
      frames = null;
      decodedPlanes = [];
      appState = errorState(error);
    }
  }

  async function loadFrames(
    id: string,
    band: number | null,
    generation?: number,
  ): Promise<void> {
    const token = generation ?? ++loadGeneration;
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
          ? await fetchFrames(
              id,
              rgb
                ? { mode: "rgb", band }
                : { mode: "timeline", band, visits: DEFAULT_VISITS },
            )
          : await fetchFrames(id, {
              mode: "epoch",
              bins: 8,
              // The compare surface is a two-epoch blink; the API can also
              // request every epoch with `epochs=`.
              epochs: `0,${Math.max(1, (detail?.epochs.length ?? 2) - 1)}`,
            });
      if (token !== loadGeneration) return;
      const buffers = await Promise.all(
        response.planes.map((plane) => fetchPlane(plane.href)),
      );
      if (token !== loadGeneration) return;
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
        stage === "timeline" && !rgb
          ? Math.min(view.visit ?? 0, response.planes.length - 1)
          : 0;
      referenceIndex = 0;
      difference = false;
      diffPlane = null;
      diffMeta = null;
    diffReference = null;
      appState = { kind: "ready", title: "Ready", message: "" };
      await tick();
      if (token !== loadGeneration) return;
      if (!setupRenderers()) return;
      uploadedKey = "";
      playing = !rgb && view.visit === undefined;
      initBlink();
      blink?.goTo(frameIndex);
      drawFrame(true);
      shareView();
    } catch (error) {
      if (token !== loadGeneration) return;
      teardownRenderers();
      frames = null;
      decodedPlanes = [];
      appState = errorState(error);
    }
  }

  async function loadTarget(id: string, fov = view.fov): Promise<void> {
    const generation = ++loadGeneration;
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
      if (generation !== loadGeneration) return;
      detail = target;
      selectedId = id;
      liveQuery = null;
      liveNotice = "";

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
      rgb = false;
      diffPlane = null;
      diffMeta = null;
    diffReference = null;
      bandValue = view.band ?? target.timeline?.default_band ?? 0;
      await loadFrames(id, bandValue, generation);
    } catch (error) {
      if (generation !== loadGeneration) return;
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
          if (liveEnabled) {
            await loadLive(shared.ra, shared.dec, shared.fov, DEFAULT_VISITS);
            return;
          }
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
    if (id === "live") {
      liveNotice = "";
      liveOpen = true;
      return;
    }
    const target = targets.find((item) => item.id === id);
    if (!target) return;

    view = { ra: view.ra, dec: view.dec, fov: view.fov };
    void loadTarget(target.id, view.fov);
  }

  function retry(): void {
    if (liveQuery) {
      void loadLive(
        liveQuery.ra,
        liveQuery.dec,
        liveQuery.fov,
        liveQuery.bins,
      );
    } else if (selectedId) {
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
    {liveEnabled}
    {liveActive}
    onTarget={onTargetChange}
    onToggleRail={() => (railOpen = !railOpen)}
    onLive={() => {
      liveNotice = "";
      liveOpen = true;
    }}
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
        planePx={frames?.source_plane_px ?? frames?.planes[0]?.width ?? 512}
        {difference}
        {notes}
        onResize={redrawForResize}
      />

      {#if difference && diffMeta?.cross_release}
        <div
          class="shrink-0 border-b border-warm/40 bg-warm/10 px-3 py-1.5 text-label text-warm"
          role="status"
          data-testid="cross-release-warning"
        >
          Cross-release difference — the visits come from different processing
          releases, so a pipeline zero-point change may be present.
        </div>
      {/if}

      {#if difference && (diffMeta?.bandpass_fraction ?? 0) > 0.25}
        <div
          class="shrink-0 border-b border-warn/40 bg-warn/10 px-3 py-1.5 text-label text-warn"
          role="status"
          data-testid="bandpass-warning"
        >
          Bandpass mismatch — the visits differ by
          {diffMeta?.bandpass_delta_um?.toFixed(3)} µm
          ({Math.round((diffMeta?.bandpass_fraction ?? 0) * 100)}% of the bin).
          Bright-star residuals are expected; this is not a sky change.
        </div>
      {/if}

      <DegradedBanner
        degraded={frames
          ? (frames.degraded_header || frames.degraded || false)
          : false}
        omitted={frames?.frames_omitted ?? []}
        shown={frames?.visits ?? frames?.plane_count ?? steps.length}
        available={frames?.available_visits ??
          (frames?.mode === "epoch"
            ? (frames?.bins ?? 0) * (frames?.epochs?.length ?? 0)
            : steps.length)}
        unit={frames?.mode === "epoch" ? "band" : "visit"}
        resolutionReduced={frames?.resolution_reduced ?? false}
        compressedBytes={frames?.compressed_bytes ?? null}
      />

      <Dock
        steps={steps}
        index={frameIndex}
        {playing}
        {difference}
        {rgb}
        {canDifference}
        {canRgb}
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
        degraded={stage === "timeline" && (frames?.degraded ?? false)}
        omitted={frames?.frames_omitted ?? []}
        shown={frames?.visits ?? steps.length}
        available={frames?.available_visits ?? steps.length}
        compare={stage === "compare"}
        disabled={!steps.length || !ready}
        onGoTo={goTo}
        onStep={step}
        onTogglePlay={togglePlayback}
        onDifference={() => void setDifference(!difference)}
        onRgb={() => void setRgb(!rgb)}
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
      reference={diffReference}
      bandpass={diffBandpass}
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

  {#if liveEnabled}
    <LiveDialog
      open={liveOpen}
      ra={view.ra}
      dec={view.dec}
      fov={view.fov}
      maxFov={manifest?.live_max_fov_deg ?? 1}
      busy={liveBusy}
      notice={liveNotice}
      onClose={() => (liveOpen = false)}
      onSubmit={(ra, dec, fov, bins) => void loadLive(ra, dec, fov, bins)}
    />
  {/if}
</div>
