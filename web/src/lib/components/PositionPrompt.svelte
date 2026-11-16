<script lang="ts">
  import PositionForm from "$lib/components/PositionForm.svelte";
  import type { TargetSummary } from "$lib/api";

  interface Props {
    liveEnabled: boolean;
    ra: number;
    dec: number;
    fov: number;
    maxFov: number;
    targets: TargetSummary[];
    busy: boolean;
    notice: string;
    onLive: (ra: number, dec: number, fov: number, bins: number) => void;
    onDemo: (id: string) => void;
  }

  let {
    liveEnabled,
    ra,
    dec,
    fov,
    maxFov,
    targets,
    busy,
    notice,
    onLive,
    onDemo,
  }: Props = $props();
</script>

<div
  class="max-h-full w-full max-w-[34rem] overflow-y-auto rounded-panel border border-line bg-surface"
  data-testid="position-prompt"
>
  <div class="border-b border-line px-4 py-3">
    <h2 class="m-0 text-title font-medium text-text">Choose a sky position</h2>
    <p class="m-0 mt-1 text-label leading-relaxed text-muted">
      Enter coordinates to fetch a live visit timeline from the archive, or open
      one of the prepared demo targets.
    </p>
  </div>

  <div class="px-4 py-3">
    {#if targets.length}
      <div class="grid gap-1.5 min-[560px]:grid-cols-3">
        {#each targets as target (target.id)}
          <button
            type="button"
            class="flex min-w-0 flex-col gap-0.5 rounded-control border border-line bg-raised px-2 py-1.5 text-left transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-40"
            title={target.name ?? target.id}
            onclick={() => onDemo(target.id)}
            disabled={busy}
          >
            <span class="truncate text-label text-text">
              {target.name ?? target.id}
            </span>
            <span class="truncate text-micro text-faint">
              {target.kind ?? "target"}
            </span>
            {#if target.ra !== null && target.dec !== null}
              <span class="truncate font-mono text-micro text-faint">
                {target.ra.toFixed(3)}°, {target.dec.toFixed(3)}°
              </span>
            {/if}
          </button>
        {/each}
      </div>
    {:else}
      <p class="m-0 text-label text-faint">No demo targets are published.</p>
    {/if}
  </div>

  {#if liveEnabled}
    <PositionForm
      {ra}
      {dec}
      {fov}
      {maxFov}
      {busy}
      {notice}
      submitLabel="View"
      onSubmit={onLive}
    />
  {:else}
    <p class="m-0 border-b border-line px-4 py-4 text-label leading-relaxed text-muted">
      Live coordinate lookup is disabled on this server. Choose a demo target
      below to browse the pinned snapshot.
    </p>
  {/if}
</div>
