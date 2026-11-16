<script lang="ts">
  import type { Snippet } from "svelte";

  import StateView from "$lib/components/StateView.svelte";
  import {
    chooseScaleBar,
    formatDate,
    formatMjd,
    type FrameFacts,
  } from "$lib/format";

  interface Props {
    canvas?: HTMLCanvasElement;
    ready: boolean;
    stateKind: string;
    stateTitle: string;
    stateMessage: string;
    retry?: () => void;
    frame: FrameFacts | null;
    caption: string[];
    altText: string;
    statusMessage: string;
    label: string;
    pixelScaleArcsec: number;
    planePx: number;
    difference: boolean;
    notes: string[];
    stateSlot?: Snippet;
    onResize: () => void;
  }

  let {
    canvas = $bindable(),
    ready,
    stateKind,
    stateTitle,
    stateMessage,
    retry,
    frame,
    caption,
    altText,
    statusMessage,
    label,
    pixelScaleArcsec,
    planePx,
    difference,
    notes,
    stateSlot,
    onResize,
  }: Props = $props();

  let areaWidth = $state(0);
  let areaHeight = $state(0);

  const canvasSize = $derived(
    Math.max(0, Math.floor(Math.min(areaWidth, areaHeight))),
  );
  const scaleBar = $derived(
    chooseScaleBar(
      pixelScaleArcsec,
      planePx,
      canvasSize,
      Math.max(56, Math.min(canvasSize * 0.3, 168)),
    ),
  );

  const frameDate = $derived(frame ? formatDate(frame.date) : null);
  const frameMjd = $derived(frame ? formatMjd(frame.mjd) : null);

  function measure(node: HTMLElement) {
    const apply = () => {
      const style = getComputedStyle(node);
      const width =
        node.clientWidth -
        parseFloat(style.paddingLeft) -
        parseFloat(style.paddingRight);
      const height =
        node.clientHeight -
        parseFloat(style.paddingTop) -
        parseFloat(style.paddingBottom);
      if (width === areaWidth && height === areaHeight) return;
      areaWidth = width;
      areaHeight = height;
    };
    apply();
    const observer = new ResizeObserver(apply);
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
  }

  $effect(() => {
    if (canvasSize > 0) onResize();
  });
</script>

<section
  class="flex min-h-0 min-w-0 flex-1 flex-col bg-void"
  aria-labelledby="stage-title"
>
  <h2 id="stage-title" class="sr-only">Matched-wavelength frame</h2>

  <figure class="m-0 flex min-h-0 flex-1 flex-col">
    <div
      class="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-void p-4"
      use:measure
    >
      {#if notes.length}
        <ul
          class="pointer-events-none absolute left-3 top-3 m-0 flex max-w-[9rem] list-none flex-col gap-1 p-0 text-micro leading-snug text-faint"
          data-testid="stage-notes"
        >
          {#each notes as note}
            <li>{note}</li>
          {/each}
        </ul>
      {/if}
      {#if ready}
        <div
          class="relative shrink-0 overflow-hidden rounded-[2px] border border-line bg-void"
          style={`--canvas-size:${canvasSize}px; width:${canvasSize}px; height:${canvasSize}px;`}
          data-testid="canvas-fit"
        >
          <canvas
            bind:this={canvas}
            tabindex="0"
            aria-label={label}
            aria-describedby="canvas-alt"
            aria-keyshortcuts="Space"
            class="block h-full w-full bg-void [image-rendering:pixelated]"
          ></canvas>

          <div
            class="pointer-events-none absolute inset-0 flex flex-col justify-between p-2.5"
            aria-hidden="true"
          >
            <div class="flex items-start justify-between gap-2">
              <div class="overlay-chip min-w-0">
                <span class="overlay-label">UTC</span>
                <span class="truncate text-fine text-text">
                  {frameDate ?? "—"}
                </span>
                <span class="text-micro text-muted">
                  {frameMjd ?? ""}
                </span>
              </div>
              <div class="overlay-chip gap-2.5" data-testid="orientation">
                <span class="flex items-center gap-1 text-fine text-text">
                  <svg viewBox="0 0 8 12" class="h-3 w-2 fill-signal">
                    <path d="M4 0 8 6H5v6H3V6H0z" />
                  </svg>
                  N
                </span>
                <span class="h-3 w-px bg-white/15"></span>
                <span class="flex items-center gap-1 text-fine text-text">
                  E
                  <svg viewBox="0 0 12 8" class="h-2 w-3 fill-signal">
                    <path d="M0 3h8V0l4 4-4 4V5H0z" />
                  </svg>
                </span>
              </div>
            </div>

            <div class="flex items-end justify-between gap-2">
              {#if scaleBar}
                <div class="overlay-chip" data-testid="scale-bar">
                  <span class="overlay-label">{scaleBar.label}</span>
                  <div class="flex items-center">
                    <span class="h-2.5 w-px bg-text/60"></span>
                    <span
                      class="h-px bg-text/60"
                      style={`width:${scaleBar.pixels}px`}
                    ></span>
                    <span class="h-2.5 w-px bg-text/60"></span>
                  </div>
                </div>
              {:else}
                <span></span>
              {/if}
              <div class="overlay-chip gap-2">
                <span class="overlay-label">FOV</span>
                <span class="text-fine text-text">
                  {(planePx || 0) > 0
                    ? `${((pixelScaleArcsec * planePx) / 3600).toFixed(2)}°`
                    : "—"}
                </span>
              </div>
            </div>
          </div>
        </div>
      {:else if stateSlot}
        {@render stateSlot()}
      {:else}
        <StateView
          state={stateKind}
          title={stateTitle}
          message={stateMessage}
          onRetry={retry}
        />
      {/if}
    </div>

    <figcaption
      class="mx-auto flex h-8 w-full shrink-0 items-center justify-center gap-2 overflow-hidden border-t border-line bg-void px-2 text-muted whitespace-nowrap"
      data-testid="caption"
    >
      {#if frame && caption.length}
        {#each caption as fact, index (index)}
          <span class={index === caption.length - 1 ? "text-text" : ""}>{fact}</span>
        {/each}
      {:else}
        <span class="text-faint">No frame loaded.</span>
      {/if}
    </figcaption>
  </figure>

  <p id="canvas-alt" class="sr-only" aria-live="polite">{altText}</p>
  <p class="sr-only" role="status" aria-live="polite">{statusMessage}</p>
</section>
