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
    type RenderMode,
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
    epoch: "Epoch compare",
    night_pair: "Night pair",
    spectrum: "Spectrum",
  };

  type ImageMode = "blink" | "difference";

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

  interface BandPair {
    a: PlaneEntry;
    b: PlaneEntry;
  }

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
  let mode = $state<ImageMode>("blink");
  let preset = $state<RenderPreset>("linear");
  let fps = $state<number>(DEFAULT_FPS);
  let playing = $state<boolean>(true);
  let bandIndex = $state<number>(0);
  let bandCount = $state<number>(0);
  let blinkFrame = $state<number>(0);
  let altText = $state<string>("No frame loaded.");
  let statusMessage = $state<string>("");
  let view = $state<ViewState>({ ra: 0, dec: 0, fov: DEFAULT_FOV });
  let canvasA = $state<HTMLCanvasElement>();

  let rendererA: PlaneRenderer | null = null;
  let blink: BlinkController | null = null;
  let uploadedKey = "";

  const pair = $derived(bandPair(bandIndex));
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

  function bandPair(index: number): BandPair | null {
    if (!frames || decodedPlanes.length !== frames.planes.length) return null;
    const entries = frames.planes.map((meta, position) => ({
      meta,
      plane: decodedPlanes[position],
    }));
    const group = entries.filter((entry) => entry.meta.band_index === index);
    if (group.length < 2) return null;
    return { a: group[0], b: group[1] };
  }

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

  function epochText(meta: PlaneMeta | undefined): string {
    if (!meta) return "—";
    return `${meta.release.toUpperCase()} · ${meta.epoch_id}`;
  }

  function currentMeta(): PlaneMeta | undefined {
    if (!pair) return undefined;
    if (mode === "difference") return pair.a.meta;
    return blinkFrame === 0 ? pair.a.meta : pair.b.meta;
  }

  function panelLabel(): string {
    if (!pair) return "No image loaded";
    if (mode === "difference") {
      return (
        `Difference image ${pair.b.meta.epoch_id} minus ${pair.a.meta.epoch_id}, ` +
        `${formatLambda(pair.a.meta)}, ${scaleText()}, ${unitsText()}`
      );
    }
    const meta = currentMeta();
    return `${meta?.epoch_id ?? "epoch"}, ${formatLambda(meta)}, ${scaleText()}, ${unitsText()}`;
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
    blinkFrame = 0;
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
    blink = new BlinkController(2, fps);
    blink.playing = playing;
    blink.onFrame((frame) => {
      if (mode !== "blink") return;
      blinkFrame = frame;
      drawFrame();
    });
    blink.start();
  }

  function drawFrame(force = false): void {
    if (!frames || !rendererA || !pair) return;
    const difference = mode === "difference";
    const key = difference ? `${bandIndex}:difference` : `${bandIndex}:${blinkFrame}`;
    if (force || uploadedKey !== key) {
      if (difference) {
        rendererA.setPlane(pair.a.plane, 0);
        rendererA.setPlane(pair.b.plane, 1);
      } else {
        const entry = blinkFrame === 0 ? pair.a : pair.b;
        rendererA.setPlane(entry.plane, 0);
      }
      uploadedKey = key;
    }
    rendererA.setWindow(frames.window);
    rendererA.setPreset(preset);
    rendererA.setMode(difference ? "difference" : "single");
    rendererA.draw();

    const meta = currentMeta();
    if (difference) {
      statusMessage =
        `Difference ${pair.b.meta.epoch_id} − ${pair.a.meta.epoch_id} · ` +
        `${formatLambda(pair.a.meta)} · one shared window`;
      altText =
        `Difference image: ${pair.b.meta.epoch_id} minus ${pair.a.meta.epoch_id}, ` +
        `${formatLambda(pair.a.meta)}, ${scaleText()}, ${unitsText()}. ` +
        "Blue means brighter in the second epoch, amber means fainter, magenta marks missing pixels. Both epochs use the same single stretch window.";
    } else {
      statusMessage =
        `Epoch ${blinkFrame + 1} of 2 · ${epochText(meta)} · ` +
        `${formatLambda(meta)} · blinking DR1 ↔ QR3`;
      altText =
        `Blink frame ${blinkFrame + 1} of 2: ${meta?.epoch_id}, ` +
        `${formatLambda(meta)}, ${scaleText()}, ${unitsText()}. ` +
        "The two matched epochs alternate at the selected speed and share one stretch window.";
    }
  }

  function setFpsValue(value: number): void {
    fps = Math.min(MAX_FPS, Math.max(MIN_FPS, Math.round(value)));
    blink?.setFps(fps);
  }

  function setBandValue(value: number): void {
    const maximum = Math.max(1, bandCount);
    bandIndex = Math.min(Math.max(1, Math.round(value)), maximum) - 1;
    uploadedKey = "";
    drawFrame(true);
  }

  function setModeValue(next: ImageMode): void {
    mode = next;
    uploadedKey = "";
    if (next === "difference") {
      playing = false;
      if (blink) blink.playing = false;
    } else {
      playing = true;
      if (blink) blink.playing = true;
    }
    drawFrame(true);
  }

  function setPresetValue(next: RenderPreset): void {
    preset = next;
    if (frames && rendererA) {
      drawFrame();
    }
  }

  function togglePlayback(): void {
    if (mode !== "blink") return;
    playing = !playing;
    if (blink) blink.playing = playing;
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

  async function loadTarget(id: string, fov = view.fov): Promise<void> {
    teardownRenderers();
    frames = null;
    decodedPlanes = [];
    bandCount = 0;
    bandIndex = 0;
    appState = {
      kind: "loading",
      title: "Loading",
      message: "Fetching matched planes and checking the shared stretch window…",
    };
    try {
      const target = await fetchTarget(id);
      detail = target;
      selectedId = id;
      view = {
        ra: target.ra ?? 0,
        dec: target.dec ?? 0,
        fov: fov,
      };
      writeShareState(view);

      if (!target.modes.epoch?.enabled) {
        appState = {
          kind: "one-epoch",
          title: "Epoch comparison unavailable",
          message:
            target.modes.epoch?.reason ??
            "This target does not have two comparable epochs.",
        };
        return;
      }

      const response = await fetchFrames(id, 8);
      const buffers = await Promise.all(
        response.planes.map((plane) => fetchPlane(plane.href)),
      );
      const planes = buffers.map((buffer) => decodeSpx1(buffer));
      frames = response;
      decodedPlanes = planes;
      bandCount = response.bins;
      bandIndex = 0;
      await tick();
      if (!setupRenderers()) return;
      appState = { kind: "ready", title: "Ready", message: "" };
      uploadedKey = "";
      blinkFrame = 0;
      initBlink();
      drawFrame(true);
    } catch (error) {
      teardownRenderers();
      frames = null;
      decodedPlanes = [];
      bandCount = 0;
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
        targets.find((target) => target.modes.epoch?.enabled) ?? targets[0];

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
        <h2 id="stage-title">Matched-wavelength epoch comparison</h2>
        {#if frames}
          <span class="note">
            {frames.bins} bands × {frames.epochs.length} epochs · one shared window
          </span>
        {/if}
      </div>
      <p class="lede">
        Wavelengths are matched before any comparison. The two matched epochs
        share one ingest-computed stretch window, the same pixel scale, and the
        same units, so a blink cannot flatter one epoch. Motion below one pixel
        is measured, not seen.
      </p>

      {#if manifest?.fixture}
        <p class="fixture-warning" role="note">
          Synthetic fixture snapshot — not archive data. It exists to exercise the
          API and renderer, and the production publish path refuses it without an
          explicit flag.
        </p>
      {/if}

      {#if pair}
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
                {mode === "difference"
                  ? `${pair.b.meta.epoch_id} − ${pair.a.meta.epoch_id}`
                  : epochText(currentMeta())}
              </strong>
              <span>λ {formatLambda(currentMeta())}</span>
              <span>{scaleText()}</span>
              <span>{unitsText()}</span>
              <span>
                {mode === "difference"
                  ? "difference"
                  : `blink · epoch ${blinkFrame + 1}/2`}
              </span>
            </figcaption>
          </figure>
        </div>
        <p class="status" role="status" aria-live="polite">{statusMessage}</p>
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
          Epoch
          <strong>
            {pair
              ? `${pair.a.meta.release.toUpperCase()} ↔ ${pair.b.meta.release.toUpperCase()}`
              : "—"}
          </strong>
        </p>
        <p>λ <strong>{pair ? formatLambda(pair.a.meta) : "—"}</strong> matched</p>
        <p>Pixel scale <strong>{scaleText()}</strong></p>
        <p>Units <strong>{unitsText()}</strong></p>
        <p>
          Window sha
          <strong>{frames ? frames.window.sha256.slice(0, 12) : "—"}</strong>
        </p>
      </section>

      <section class="controls" aria-label="Blink and stretch controls">
        <div>
          <label for="fps">Blink speed <output>{fps} fps</output></label>
          <input
            id="fps"
            type="range"
            min={MIN_FPS}
            max={MAX_FPS}
            step="1"
            value={fps}
            aria-valuetext={`${fps} frames per second`}
            oninput={(event) => setFpsValue(Number(event.currentTarget.value))}
            disabled={!pair || mode === "difference"}
          />
        </div>
        <div>
          <button
            type="button"
            onclick={togglePlayback}
            aria-pressed={playing}
            disabled={!pair || mode === "difference"}
          >
            {mode === "difference"
              ? "Blink paused"
              : playing
                ? "Pause blink"
                : "Play blink"}
          </button>
          <p class="note">
            Space toggles the epoch blink when focus is not in a control.
          </p>
        </div>
        <div>
          <label for="band">
            Wavelength band
            <output>
              {bandIndex + 1}/{bandCount || 1} ·
              {formatLambda(pair ? pair.a.meta : undefined)}
            </output>
          </label>
          <input
            id="band"
            type="range"
            min="1"
            max={Math.max(1, bandCount)}
            step="1"
            value={bandIndex + 1}
            aria-valuetext={`Band ${bandIndex + 1} of ${bandCount || 1}`}
            oninput={(event) => setBandValue(Number(event.currentTarget.value))}
            disabled={!pair}
          />
        </div>
        <fieldset>
          <legend>Mode</legend>
          <div class="mode-buttons">
            <button
              type="button"
              aria-pressed={mode === "blink"}
              onclick={() => setModeValue("blink")}
              disabled={!pair}
            >
              Blink
            </button>
            <button
              type="button"
              aria-pressed={mode === "difference"}
              onclick={() => setModeValue("difference")}
              disabled={!pair}
            >
              Difference
            </button>
          </div>
        </fieldset>
        <div>
          <label for="preset">Stretch preset</label>
          <select
            id="preset"
            value={preset}
            onchange={(event) =>
              setPresetValue(event.currentTarget.value as RenderPreset)}
            disabled={!pair}
          >
            <option value="linear">Linear</option>
            <option value="asinh">Asinh</option>
            <option value="log">Log</option>
          </select>
        </div>
        <p class="note">
          Linear, asinh and log all derive from the single snapshot window
          (lo, hi, s); changing preset never recomputes the window.
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
        </dl>
        <p class="note">
          The URL query <code>?ra=&amp;dec=&amp;fov=</code> restores this view on
          reload and can be shared.
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
