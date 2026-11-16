<script lang="ts">
  import HelpDialog from "$lib/components/HelpDialog.svelte";
  import type { ManifestSummary, TargetSummary } from "$lib/api";
  import type { ViewState } from "$lib/ShareState";

  interface Props {
    manifest: ManifestSummary | null;
    snapshotAge: number | null;
    targets: TargetSummary[];
    selectedId: string;
    railOpen: boolean;
    liveEnabled: boolean;
    liveActive: boolean;
    view: ViewState;
    hasPosition: boolean;
    onTarget: (id: string) => void;
    onToggleRail: () => void;
    onCoordinates: () => void;
  }

  let {
    manifest,
    snapshotAge,
    targets,
    selectedId,
    railOpen,
    liveEnabled,
    liveActive,
    view,
    hasPosition,
    onTarget,
    onToggleRail,
    onCoordinates,
  }: Props = $props();

  const button =
    "inline-flex h-7 items-center gap-1.5 rounded-control border border-line bg-raised px-2 text-label leading-none text-muted transition-colors hover:bg-hover hover:text-text disabled:cursor-not-allowed disabled:opacity-40";
  const coordinateButton =
    "inline-flex h-7 items-center gap-1.5 rounded-control border border-signal/60 bg-signal/10 px-2 text-label leading-none text-signal transition-colors hover:bg-signal/20";

  let copied = $state(false);
  let copyTimer: ReturnType<typeof setTimeout> | null = null;
  let helpOpen = $state(false);

  const selected = $derived(
    targets.find((target) => target.id === selectedId) ?? null,
  );
  const selectValue = $derived(liveActive ? "" : selectedId);

  function formatAge(seconds: number | null): string {
    if (seconds === null || seconds === undefined) return "Age unavailable";
    if (seconds < 120) return `${Math.round(seconds)} s old`;
    if (seconds < 7200) return `${Math.round(seconds / 60)} min old`;
    if (seconds < 172800) return `${Math.round(seconds / 3600)} h old`;
    return `${Math.round(seconds / 86400)} d old`;
  }

  function targetLabel(target: TargetSummary): string {
    return target.name ?? target.id;
  }

  async function shareView(): Promise<void> {
    try {
      await navigator.clipboard.writeText(window.location.href);
    } catch {}
    copied = true;
    if (copyTimer) clearTimeout(copyTimer);
    copyTimer = setTimeout(() => {
      copied = false;
    }, 1800);
  }
</script>

<header
  class="relative z-20 flex h-12 shrink-0 items-center gap-2.5 border-b border-line bg-surface px-3"
  data-testid="header"
>
  <div class="flex shrink-0 items-baseline gap-2">
    <h1 class="m-0 text-title font-medium text-text">DRIFT</h1>
    <span class="hidden text-micro text-faint min-[1420px]:inline">
      Differential Infrared Frame Tracker
    </span>
  </div>

  <span class="h-5 w-px shrink-0 bg-line" aria-hidden="true"></span>

  {#if liveEnabled}
    <button
      type="button"
      class={coordinateButton}
      aria-haspopup="dialog"
      title="Change the sky position. Coordinates are looked up in the live archive."
      data-testid="coordinates-button"
      onclick={onCoordinates}
    >
      <svg
        viewBox="0 0 12 12"
        class="h-3 w-3 fill-none stroke-current [stroke-width:1.4]"
        aria-hidden="true"
      >
        <circle cx="6" cy="6" r="1.6" class="fill-current stroke-none" />
        <path d="M6 0.8v2M6 9.2v2M0.8 6h2M9.2 6h2" />
      </svg>
      <span>Position</span>
      {#if hasPosition}
        <span class="hidden font-mono text-fine text-muted min-[1240px]:inline">
          {view.ra.toFixed(2)}°, {view.dec.toFixed(2)}°
        </span>
      {/if}
      <span class="text-signal">Change</span>
    </button>
  {/if}

  {#if liveEnabled}
    <span class="h-5 w-px shrink-0 bg-line" aria-hidden="true"></span>
  {/if}

  <div
    class="flex min-w-0 items-center gap-2 text-fine text-faint"
    data-state={manifest ? "ready" : "loading"}
    data-fixture={manifest?.fixture ?? false}
    id="snapshot-badge"
  >
    {#if manifest}
      {#if manifest.fixture}
        <span
          class="shrink-0 rounded-control border border-warm/50 bg-warm/10 px-1.5 py-px text-micro text-warm"
        >
          Fixture
        </span>
      {/if}
      <span class="truncate text-muted" title={manifest.snapshot_id}>
        {manifest.snapshot_id}
      </span>
      <span class="hidden shrink-0 min-[1080px]:inline">{formatAge(snapshotAge)}</span>
    {:else}
      <span>Loading snapshot…</span>
    {/if}
  </div>

  <div class="ml-auto flex shrink-0 items-center gap-2">
    <label class="section-label shrink-0" for="target-select">Demo</label>
    <div class="relative">
      <select
        id="target-select"
        class="h-7 w-[13rem] appearance-none rounded-control border border-line bg-raised px-2 pr-6 text-fine text-text"
        value={selectValue}
        title={selected ? `Demo target: ${targetLabel(selected)} — ${selectedId}` : "Choose a demo target"}
        onchange={(event) => onTarget(event.currentTarget.value)}
      >
        {#if !selectedId || liveActive}
          <option value="" disabled>Select a demo target…</option>
        {/if}
        {#each targets as target (target.id)}
          <option value={target.id} title={targetLabel(target)}>
            {targetLabel(target)}
          </option>
        {/each}
      </select>
      <svg
        class="pointer-events-none absolute right-2 top-1/2 h-2 w-2 -translate-y-1/2 fill-faint"
        viewBox="0 0 8 8"
        aria-hidden="true"
      >
        <path d="M0 2h8L4 7z" />
      </svg>
    </div>
    <span class="h-5 w-px shrink-0 bg-line" aria-hidden="true"></span>
    <button
      type="button"
      class={button}
      aria-haspopup="dialog"
      title="What the labels, controls and keys mean."
      onclick={() => (helpOpen = true)}
    >
      <svg
        viewBox="0 0 12 12"
        class="h-3 w-3 fill-none stroke-current [stroke-width:1.3]"
        aria-hidden="true"
      >
        <circle cx="6" cy="6" r="4.6" />
        <path d="M4.4 4.4a1.7 1.7 0 1 1 2.2 1.7c-.5.2-.6.6-.6 1" />
        <circle cx="6" cy="8.9" r="0.55" class="fill-current stroke-none" />
      </svg>
      Help
    </button>
    <button
      type="button"
      class={button}
      aria-live="polite"
      title="Copy a view URL pinned to this target, band and visit."
      onclick={() => void shareView()}
    >
      {#if copied}
        <svg viewBox="0 0 12 12" class="h-3 w-3 fill-none stroke-signal [stroke-width:1.6]" aria-hidden="true">
          <path d="M1.5 6.5 4.5 9.5 10.5 2.5" />
        </svg>
        <span class="text-signal">Copied</span>
      {:else}
        <svg viewBox="0 0 12 12" class="h-3 w-3 fill-none stroke-current [stroke-width:1.3]" aria-hidden="true">
          <path d="M4 1.5H2.5v8H10V8" />
          <path d="M6 1.5h4.5V6M10.5 1.5 5 7" />
        </svg>
        Share
      {/if}
    </button>
    <button
      type="button"
      class={button}
      aria-expanded={railOpen}
      aria-controls="inspector-rail"
      onclick={onToggleRail}
    >
      {railOpen ? "Hide" : "Show"}
    </button>
  </div>

  <HelpDialog open={helpOpen} onClose={() => (helpOpen = false)} />
</header>
