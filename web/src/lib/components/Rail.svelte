<script lang="ts">
  import Histogram from "$lib/components/Histogram.svelte";
  import Info from "$lib/components/Info.svelte";
  import ModeTabs from "$lib/components/ModeTabs.svelte";
  import type {
    FramesResponse,
    ReferenceVisit,
    TargetDetail,
    WindowInfo,
  } from "$lib/api";
  import {
    formatDate,
    formatMjd,
    readoutRows,
    releaseLabel,
    type StackFacts,
  } from "$lib/format";
  import type { RenderPreset } from "$lib/PlaneRenderer";
  import type { HistogramData } from "$lib/histogram";
  import type { ViewState } from "$lib/ShareState";

  interface ModeRow {
    key: string;
    label: string;
    enabled: boolean;
    reason: string;
  }

  interface Props {
    detail: TargetDetail | null;
    frames: FramesResponse | null;
    stack: StackFacts | null;
    reference: ReferenceVisit | null;
    bandpass: { deltaUm: number; fraction: number } | null;
    modes: ModeRow[];
    view: ViewState;
    citation: string;
    band: number | null;
    stage: "timeline" | "compare";
    tone: RenderPreset;
    histogram: HistogramData | null;
    window: WindowInfo | null;
    sourceUrl: string | null;
    open: boolean;
    onStage: (stage: "timeline" | "compare") => void;
  }

  let {
    detail,
    frames,
    stack,
    reference,
    bandpass,
    modes,
    view,
    citation,
    band,
    stage,
    tone,
    histogram,
    window: stretchWindow,
    sourceUrl,
    open,
    onStage,
  }: Props = $props();

  const RAIL_WIDTH = 344;

  const rows = $derived(stack ? readoutRows(stack) : []);
  const windowTitle = $derived(
    frames ? `Window lo ${frames.window.lo}, hi ${frames.window.hi}, s ${frames.window.s}` : "",
  );
</script>

<aside
  id="inspector-rail"
  class="min-h-0 shrink-0 overflow-hidden bg-surface transition-[width,opacity] duration-200 ease-out {open
    ? 'border-l border-line'
    : ''}"
  style={`width:${open ? RAIL_WIDTH : 0}px; opacity:${open ? 1 : 0};`}
  aria-label="Target and stack details"
  aria-hidden={!open}
  inert={!open}
  data-testid="rail"
>
  <div class="flex h-full w-[344px] flex-col">
    <div class="shrink-0 border-b border-line px-3 py-2.5">
      <div class="mb-2 flex items-center justify-between">
        <span class="section-label">Mode</span>
        <span class="text-fine text-faint">
          {stage === "compare" ? "Epoch compare" : "Visit timeline"}
        </span>
      </div>
      <ModeTabs {modes} {stage} {onStage} />
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto">
      <section class="border-b border-line px-3 py-3" aria-labelledby="rail-identity">
        <div class="mb-2 flex items-baseline justify-between">
          <span class="flex items-center gap-1.5">
            <h2 id="rail-identity" class="section-label">Identity</h2>
            <Info
              label="Identity"
              text="The target's sky position, field of view and number of processing epochs."
            />
          </span>
          <span class="text-fine text-faint">
            {detail?.kind ?? "target"}
          </span>
        </div>
        {#if detail}
          <p class="m-0 truncate text-body font-medium text-text" title={detail.name ?? detail.id}>
            {detail.name ?? detail.id}
          </p>
          <p class="m-0 mb-2 truncate text-fine text-faint" title={detail.id}>
            {detail.id}
          </p>
          <dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-label">
            <div class="flex flex-col gap-0.5">
              <dt class="text-faint">RA</dt>
              <dd class="m-0 text-text">
                {detail.ra === null ? "—" : `${detail.ra.toFixed(5)}°`}
              </dd>
            </div>
            <div class="flex flex-col gap-0.5">
              <dt class="text-faint">Dec</dt>
              <dd class="m-0 text-text">
                {detail.dec === null ? "—" : `${detail.dec.toFixed(5)}°`}
              </dd>
            </div>
            <div class="flex flex-col gap-0.5">
              <dt class="text-faint">Field of view</dt>
              <dd class="m-0 text-text">{view.fov.toFixed(2)}°</dd>
            </div>
            <div class="flex flex-col gap-0.5">
              <dt class="text-faint">Epochs</dt>
              <dd class="m-0 text-text">{detail.epochs.length}</dd>
            </div>
          </dl>
        {:else}
          <p class="text-label text-faint">No target loaded for this view.</p>
        {/if}
      </section>

      <section class="border-b border-line px-3 py-3" aria-labelledby="rail-readout">
        <div class="mb-2 flex items-baseline justify-between">
          <span class="flex items-center gap-1.5">
            <h2 id="rail-readout" class="section-label">Details</h2>
            <Info
              label="Details"
              text="Key details for this stack: detector, wavelength range, pixel scale, units, window hash, and number of visits."
            />
          </span>
          <span
            class="rounded-[2px] border border-signal/40 bg-signal/10 px-1.5 py-0.5 text-fine text-signal"
            data-testid="readout-band">B{band ?? "—"}</span
          >
        </div>
        {#if detail?.timeline?.band_reason}
          <p
            class="m-0 mb-2 text-fine leading-snug text-faint"
            data-testid="band-reason"
          >
            {detail.timeline.band_reason}
          </p>
        {/if}
        <dl class="flex flex-col gap-1 text-label">
          {#each rows as item (item.label)}
            <div
              class="flex items-baseline justify-between gap-2 border-b border-line/60 pb-1 last:border-b-0 last:pb-0"
              title={item.label === "Window hash" ? windowTitle : item.detail}
            >
              <dt class="text-faint">{item.label}</dt>
              <dd class="m-0 text-right text-text">{item.value}</dd>
            </div>
          {/each}
          {#if sourceUrl}
            <div class="flex items-baseline justify-between gap-2 border-b border-line/60 pb-1">
              <dt class="text-faint">Source</dt>
              <dd class="m-0 text-right text-text">
                <a
                  class="text-signal underline-offset-2 hover:underline"
                  href={sourceUrl}
                  target="_blank"
                  rel="noreferrer"
                  title={sourceUrl}>this visit's cutout</a
                >
              </dd>
            </div>
          {/if}
          {#if reference}
            <div class="flex items-baseline justify-between gap-2 border-b border-line/60 pb-1">
              <dt class="text-faint">Difference ref</dt>
              <dd
                class="m-0 text-right text-text"
                title={`${reference.visit_id} · ${formatDate(reference.date)} · ${formatMjd(reference.mjd)} · ${releaseLabel(reference.release)}`}
              >
                {formatDate(reference.date).slice(0, 10)}
                <span class="text-faint">
                  · {releaseLabel(reference.release)}
                </span>
              </dd>
            </div>
          {/if}
          {#if bandpass}
            <div class="flex items-baseline justify-between gap-2 border-b border-line/60 pb-1">
              <dt class="text-faint">Bandpass Δ</dt>
              <dd
                class="m-0 text-right {bandpass.fraction > 0.25
                  ? 'text-warn'
                  : 'text-text'}"
                title="Measured wavelength difference between this visit and its reference."
              >
                {bandpass.deltaUm.toFixed(3)} µm
                <span class="text-faint">
                  · {Math.round(bandpass.fraction * 100)}% of bin
                </span>
              </dd>
            </div>
          {/if}
          {#if frames?.mode === "timeline"}
            <div class="flex items-baseline justify-between gap-2 pt-1.5">
              <dt class="text-faint">Stack</dt>
              <dd class="m-0 text-right text-text">
                {frames?.visits ?? 0}/{frames?.available_visits ?? 0} visits
                <span class="text-faint">
                  over {frames?.span_days === null || frames?.span_days === undefined
                    ? "—"
                    : `${frames.span_days.toFixed(2)} d`}
                </span>
              </dd>
            </div>
          {/if}
        </dl>
      </section>

      <section class="border-b border-line px-3 py-3" aria-labelledby="rail-tone">
        <div class="mb-2 flex items-center justify-between">
          <span class="flex items-center gap-1.5">
            <h2 id="rail-tone" class="section-label">Tone</h2>
            <Info
              label="Tone"
              text="Display curve applied to the shared stretch window. Asinh is the default."
            />
          </span>
          <span class="text-fine text-warm capitalize">{tone}</span>
        </div>
        <Histogram data={histogram} window={stretchWindow} {tone} />
      </section>

      <section class="px-3 py-3" aria-labelledby="rail-notes">
        <div class="mb-2 flex items-baseline justify-between">
          <h2 id="rail-notes" class="section-label">
            Credits
          </h2>
          <a
            class="text-fine text-signal underline-offset-2 hover:underline"
            href="https://github.com/rsa16/drift">AGPL-3.0</a
          >
        </div>
        <div class="well mt-2.5 rounded-[3px] px-2 py-1.5">
          <p class="m-0 text-micro leading-relaxed text-faint">{citation}</p>
          <p class="m-0 mt-2 text-micro leading-relaxed text-faint">
            SPHEREx via IRSA, Gaia DR3, AllWISE/NEOWISE, SkyBoT/IMCCE,
            Astropy, reproject
          </p>
        </div>
      </section>
    </div>
  </div>
</aside>
