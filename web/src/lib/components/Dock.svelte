<script lang="ts">
  import { tick } from "svelte";

  import type { PlaneMeta } from "$lib/api";
  import Segmented from "$lib/components/Segmented.svelte";
  import type { RenderPreset } from "$lib/PlaneRenderer";
  import {
    cadencePositions,
    epochGroups,
    formatLambda,
    frameFacts,
    stepAccessibleName,
    stepShortLabel,
    type BandTick,
  } from "$lib/format";

  interface Props {
    steps: { meta: PlaneMeta }[];
    index: number;
    playing: boolean;
    difference: boolean;
    rgb: boolean;
    canDifference: boolean;
    canRgb: boolean;
    tone: RenderPreset;
    bands: number[];
    band: number;
    compareBands: BandTick[];
    bandIndex: number;
    referenceIndex: number;
    fps: number;
    minFps: number;
    maxFps: number;
    degraded: boolean;
    omitted: string[];
    shown: number;
    available: number;
    compare: boolean;
    disabled: boolean;
    onGoTo: (index: number) => void;
    onStep: (delta: number) => void;
    onTogglePlay: () => void;
    onDifference: () => void;
    onRgb: () => void;
    onReference: (index: number) => void;
    onTone: (tone: RenderPreset) => void;
    onBand: (band: number) => void;
    onCompareBand: (index: number) => void;
    onFps: (fps: number) => void;
  }

  let {
    steps,
    index,
    playing,
    difference,
    rgb,
    canDifference,
    canRgb,
    tone,
    bands,
    band,
    compareBands,
    bandIndex,
    referenceIndex,
    fps,
    minFps,
    maxFps,
    degraded,
    omitted,
    shown,
    available,
    compare,
    disabled,
    onGoTo,
    onStep,
    onTogglePlay,
    onDifference,
    onRgb,
    onReference,
    onTone,
    onBand,
    onCompareBand,
    onFps,
  }: Props = $props();

  const button =
    "inline-flex h-7 items-center justify-center rounded-control border border-line bg-raised px-2 text-label leading-none text-muted transition-colors hover:bg-hover hover:text-text disabled:cursor-not-allowed disabled:opacity-40";

  const TONES: { value: string; label: string }[] = [
    { value: "linear", label: "Linear" },
    { value: "asinh", label: "Asinh" },
    { value: "log", label: "Log" },
  ];

  const cadence = $derived(
    steps.map((step) => {
      const facts = frameFacts(step.meta);
      return { release: facts?.release ?? "—", mjd: facts?.mjd ?? null };
    }),
  );
  const positions = $derived(cadencePositions(cadence));
  const groups = $derived(epochGroups(cadence, positions));
  const fpsFill = $derived(
    maxFps > minFps ? ((fps - minFps) / (maxFps - minFps)) * 100 : 0,
  );
  const labelPosition = (pct: number) => Math.min(92, Math.max(8, pct));

  function rangeExtent(group: { start: number; end: number }): {
    left: number;
    width: number;
  } {
    const start = positions[group.start] ?? 0;
    const end = positions[group.end] ?? 0;
    const width = Math.max(4, end - start);
    const left = Math.min(
      Math.max((start + end) / 2 - width / 2, 0),
      100 - width,
    );
    return { left, width };
  }

  function releaseTitle(release: string): string {
    if (release === "DR1") return "DR1 — Data Release 1 (reprocessed)";
    if (release === "QR3") return "QR3 — Quick Release 3 (minimally validated)";
    return release;
  }

  let stripEl = $state<HTMLElement>();

  async function onStripKey(
    event: KeyboardEvent,
    position: number,
  ): Promise<void> {
    const last = steps.length - 1;
    let next = -1;
    if (event.key === "ArrowLeft") next = Math.max(0, position - 1);
    else if (event.key === "ArrowRight") next = Math.min(last, position + 1);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = last;
    else return;
    event.preventDefault();
    event.stopPropagation();
    if (next === position || next < 0) return;
    onGoTo(next);
    await tick();
    stripEl
      ?.querySelector<HTMLButtonElement>(`[data-visit-step="${next}"]`)
      ?.focus();
  }
</script>

<section
  class="shrink-0 border-t border-line bg-surface"
  aria-label="Timeline and transport"
  data-testid="dock"
>
  <div class="flex flex-col gap-1.5 px-3 py-2">
    <div
      class="grid grid-cols-[2.75rem_minmax(0,1fr)] grid-rows-[20px_32px] items-center gap-x-2"
      data-testid="cadence"
    >
      <span class="section-label" aria-hidden="true">Ranges</span>
      <div
        class="relative h-5 select-none"
        aria-hidden="true"
        data-testid="ranges"
      >
        {#each groups as group (group.release + group.start)}
          {@const extent = rangeExtent(group)}
          <span
            class="absolute inset-y-0 rounded-[2px] bg-signal/[0.06]"
            style={`left:${extent.left}%; width:${extent.width}%`}
          ></span>
          <span
            class="absolute top-0 h-px bg-muted/40"
            style={`left:${extent.left}%; width:${extent.width}%`}
          ></span>
          <span
            class="absolute top-0 h-[7px] w-px bg-muted/40"
            style={`left:${extent.left}%`}
          ></span>
          <span
            class="absolute top-0 h-[7px] w-px bg-muted/40"
            style={`left:${extent.left + extent.width}%`}
          ></span>
        {/each}
        {#each groups as group (group.release + group.start)}
          {@const mid =
            ((positions[group.start] ?? 0) + (positions[group.end] ?? 0)) / 2}
          <span
            class="absolute top-[4px] -translate-x-1/2 whitespace-nowrap text-micro text-faint"
            style={`left:${labelPosition(mid)}%`}
            title={releaseTitle(group.release)}
          >
            {group.release}
            <span class="text-faint/70">({group.end - group.start + 1})</span>
          </span>
        {/each}
        {#each groups.slice(1) as group (group.release + group.start)}
          {@const gapMid = (group.gapFromPct + group.gapToPct) / 2}
          <span
            class="absolute top-[4px] -translate-x-1/2 whitespace-nowrap text-micro text-warm"
            style={`left:${labelPosition(gapMid)}%`}
            data-testid="epoch-gap"
            title="Time between the last visit of one processing release and the first visit of the next."
          >
            {group.gapDays === null ? "gap" : `${group.gapDays} d gap`}
          </span>
        {/each}
      </div>

      <span class="section-label self-center" aria-hidden="true">Visits</span>
      <div
        bind:this={stripEl}
        class="relative h-8 min-w-0"
        role="group"
        aria-label="Visit timeline"
        data-testid="strip"
      >
        {#each groups as group (group.release + group.start)}
          {@const extent = rangeExtent(group)}
          <span
            class="absolute inset-y-0 rounded-[2px] bg-signal/[0.05]"
            style={`left:${extent.left}%; width:${extent.width}%`}
            aria-hidden="true"
          ></span>
        {/each}
        <span
          class="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-line-strong"
          aria-hidden="true"
        ></span>
        {#each groups.slice(1) as group (group.release + group.start)}
          {@const gapMid = (group.gapFromPct + group.gapToPct) / 2}
          <span
            class="absolute bottom-0 top-0 border-l border-dashed border-warm/40"
            style={`left:${gapMid}%`}
            aria-hidden="true"
          ></span>
        {/each}

        {#each steps as step, position (step.meta.visit_id ??
          step.meta.epoch_id ??
          position)}
          {@const stepFacts = frameFacts(step.meta)}
          {@const active = position === index}
          {@const reference = referenceIndex === position}
          <button
            type="button"
            data-visit-step={position}
            class="group absolute top-0 z-10 flex h-8 w-5 -translate-x-1/2 items-center justify-center rounded-[2px] focus-visible:outline-offset-1"
            style={`left:${positions[position] ?? 0}%`}
            aria-current={active ? "step" : undefined}
            aria-label={stepAccessibleName(stepFacts, position)}
            title={stepAccessibleName(stepFacts, position)}
            aria-keyshortcuts="ArrowLeft ArrowRight Home End"
            tabindex={position === index ? 0 : -1}
            onclick={() => onGoTo(position)}
            onkeydown={(event) => void onStripKey(event, position)}
          >
            <span
              class="block w-[2px] rounded-full transition-all duration-150 {active
                ? 'h-[22px] bg-signal'
                : 'h-[11px] bg-line-strong group-hover:h-[16px] group-hover:bg-muted'}"
              aria-hidden="true"
            ></span>
            {#if reference && !active}
              <span
                class="absolute bottom-0 h-1 w-1 rounded-full bg-warm"
                aria-hidden="true"
              ></span>
            {/if}
          </button>
        {/each}

        <div
          class="pointer-events-none absolute top-0 z-20 h-8 w-px -translate-x-1/2 bg-signal"
          style={`left:${positions[index] ?? 0}%`}
          data-testid="playhead"
          aria-hidden="true"
        ></div>
      </div>
    </div>

    <div class="flex min-w-0 flex-wrap items-center justify-between gap-x-2.5 gap-y-1.5">
      <div class="flex items-center gap-1" role="group" aria-label="Transport">
        <button
          type="button"
          class={button + " w-7"}
          aria-label="Previous visit"
          disabled={disabled || rgb || index <= 0}
          onclick={() => onStep(-1)}
        >
          <svg viewBox="0 0 12 12" class="h-3 w-3 fill-none stroke-current [stroke-width:1.4]" aria-hidden="true">
            <path d="M7.5 2 3.5 6l4 4" />
          </svg>
        </button>
        <button
          type="button"
          class={button + " w-9"}
          aria-pressed={playing}
          aria-label={playing ? "Pause" : "Play"}
          disabled={disabled || difference || rgb}
          onclick={onTogglePlay}
        >
          {#if playing}
            <svg viewBox="0 0 12 12" class="h-3 w-3 fill-current text-signal" aria-hidden="true">
              <rect x="3" y="2" width="2" height="8" />
              <rect x="7" y="2" width="2" height="8" />
            </svg>
          {:else}
            <svg viewBox="0 0 12 12" class="h-3 w-3 fill-current text-signal" aria-hidden="true">
              <path d="M3 2v8l7-4z" />
            </svg>
          {/if}
        </button>
        <button
          type="button"
          class={button + " w-7"}
          aria-label="Next visit"
          disabled={disabled || rgb || index >= steps.length - 1}
          onclick={() => onStep(1)}
        >
          <svg viewBox="0 0 12 12" class="h-3 w-3 fill-none stroke-current [stroke-width:1.4]" aria-hidden="true">
            <path d="M4.5 2l4 4-4 4" />
          </svg>
        </button>
        <output
          class="ml-1 min-w-[3.25rem] text-label text-muted"
          aria-label="Current step"
        >
          <span class="text-text">{steps.length ? index + 1 : 0}</span>
          <span class="text-faint"> / {steps.length}</span>
        </output>
        {#if degraded}
          <span
            class="ml-1 inline-flex h-7 items-center rounded-control border border-warn/50 bg-warn/10 px-1.5 text-fine text-warn"
            title={`Omitted visit ids: ${omitted.join(", ")}`}
            aria-label={`Degraded: ${omitted.length} ${
              omitted.length === 1 ? "visit" : "visits"
            } omitted: ${omitted.join(", ")}`}
            data-testid="degraded-chip"
          >
            {shown}/{available}
          </span>
        {/if}
      </div>

      <div class="flex items-center gap-2">
        <label class="sr-only" for="reference-select">Reference visit</label>
        <select
          id="reference-select"
          data-testid="reference-chip"
          class="h-7 w-[5.5rem] rounded-control border border-warm/50 bg-raised px-1.5 text-fine text-warm disabled:cursor-not-allowed disabled:opacity-40"
          value={String(referenceIndex)}
          disabled={disabled || rgb || steps.length < 2}
          title="Reference visit for Difference. Difference is unavailable while the current step is the reference."
          onchange={(event) => onReference(Number(event.currentTarget.value))}
        >
          {#each steps as step, position (step.meta.visit_id ??
            step.meta.epoch_id ??
            position)}
            <option
              value={String(position)}
              title={stepShortLabel(frameFacts(step.meta), compare)}
            >
              REF {position + 1}
            </option>
          {/each}
        </select>

        <button
          type="button"
          class={button +
            (difference
              ? " border-signal/60 bg-signal/15 text-signal hover:bg-signal/20 hover:text-signal"
              : "")}
          aria-pressed={difference}
          disabled={disabled || rgb || !canDifference}
          title={canDifference
            ? "Compare this step with the reference visit."
            : "This step is the reference visit; choose another step to compare."}
          onclick={onDifference}
        >
          Difference
        </button>

        <button
          type="button"
          class={button +
            (rgb
              ? " border-signal/60 bg-signal/15 text-signal hover:bg-signal/20 hover:text-signal"
              : "")}
          aria-pressed={rgb}
          disabled={disabled || !canRgb}
          title={canRgb
            ? "Compose three visits from this band as a temporal RGB."
            : "Temporal RGB needs three visits at one matched band."}
          onclick={onRgb}
          data-testid="rgb-toggle"
        >
          RGB
        </button>
      </div>

      <div class="contents">
        <div class="flex items-center gap-1.5">
          <span
            class="section-label"
            id="tone-label"
            title="Display curve applied to the one shared window. Asinh is the default."
          >Tone</span>
          <Segmented
            options={TONES}
            value={tone}
            label="Tone mapping"
            compact
            testId="stretch-preset"
            onSelect={(next) => onTone(next as RenderPreset)}
          />
        </div>

        <div class="flex items-center gap-1.5">
          <label
            class="section-label"
            for="band-select"
            title="Matched wavelength bin. Changing it refetches only that band."
          >Band</label>
          <select
            id="band-select"
            class="h-7 rounded-control border border-line bg-raised px-1.5 text-label text-text disabled:opacity-40"
            value={String(band)}
            disabled={disabled || compare || bands.length < 2}
            onchange={(event) => onBand(Number(event.currentTarget.value))}
          >
            {#each bands as value (value)}
              <option value={String(value)}>B{value}</option>
            {/each}
          </select>
        </div>

        <div class="flex items-center gap-1.5">
          <label
            class="section-label"
            for="fps-range"
            title="Playback speed in frames per second."
          >Speed</label>
          <div class="relative flex h-[18px] w-[76px] items-center">
            <div
              class="pointer-events-none absolute inset-x-[1px] bottom-0 flex justify-between"
              aria-hidden="true"
            >
              {#each [0, 1, 2, 3, 4, 5, 6] as tickIndex (tickIndex)}
                <span
                  class="block w-px {tickIndex === 3
                    ? 'h-[5px] bg-line-strong'
                    : 'h-[3px] bg-line'}"
                ></span>
              {/each}
            </div>
            <input
              id="fps-range"
              type="range"
              class="fader"
              style={`--fill:${fpsFill}%`}
              min={minFps}
              max={maxFps}
              step="1"
              value={fps}
              aria-valuetext={`${fps} frames per second`}
              disabled={disabled || difference || rgb}
              oninput={(event) => onFps(Number(event.currentTarget.value))}
            />
          </div>
          <output
            class="w-[3.25rem] whitespace-nowrap text-right text-label text-muted"
            for="fps-range"
          >
            {fps}<span class="text-faint"> fps</span>
          </output>
        </div>
      </div>
    </div>

    {#if compare && compareBands.length}
      <div
        class="flex min-w-0 items-center gap-2"
        role="group"
        aria-label="Matched bands"
        data-testid="compare-bands"
      >
        <span class="section-label shrink-0">Match bands</span>
        <div class="relative flex min-w-0 flex-1 items-stretch gap-1">
          {#each compareBands as tick (tick.index)}
            <button
              type="button"
              class="min-w-0 flex-1 overflow-hidden rounded-control border px-1 py-0.5 text-micro leading-none transition-colors {bandIndex ===
              tick.index
                ? 'border-signal/60 bg-signal/15 text-signal'
                : 'border-line bg-raised text-muted hover:bg-hover hover:text-text'}"
              aria-pressed={bandIndex === tick.index}
              aria-label={`Band ${tick.band}, ${formatLambda(
                tick.lambdaLoUm,
                tick.lambdaHiUm,
              )}`}
              title={`Band ${tick.band}, ${formatLambda(
                tick.lambdaLoUm,
                tick.lambdaHiUm,
              )}`}
              data-testid={`band-tick-${tick.index}`}
              disabled={disabled}
              onclick={() => onCompareBand(tick.index)}
            >
              B{tick.band}
            </button>
          {/each}
        </div>
      </div>
    {/if}
  </div>
</section>
