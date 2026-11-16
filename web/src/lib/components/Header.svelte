<script lang="ts">
  import HelpDialog from "$lib/components/HelpDialog.svelte";
  import type { ManifestSummary, TargetSummary } from "$lib/api";

  interface Props {
    manifest: ManifestSummary | null;
    snapshotAge: number | null;
    targets: TargetSummary[];
    selectedId: string;
    railOpen: boolean;
    liveEnabled: boolean;
    liveActive: boolean;
    onTarget: (id: string) => void;
    onToggleRail: () => void;
    onLive: () => void;
  }

  let {
    manifest,
    snapshotAge,
    targets,
    selectedId,
    railOpen,
    liveEnabled,
    liveActive,
    onTarget,
    onToggleRail,
    onLive,
  }: Props = $props();

  const button =
    "inline-flex h-7 items-center gap-1.5 rounded-control border border-line bg-raised px-2 text-label leading-none text-muted transition-colors hover:bg-hover hover:text-text disabled:cursor-not-allowed disabled:opacity-40";

  let copied = $state(false);
  let copyTimer: ReturnType<typeof setTimeout> | null = null;
  let helpOpen = $state(false);

  const selected = $derived(
    targets.find((target) => target.id === selectedId) ?? null,
  );
  const selectedLabel = $derived(
    liveActive
      ? "Live sky position"
      : selected
        ? targetLabel(selected)
        : "",
  );

  function formatAge(seconds: number | null): string {
    if (seconds === null || seconds === undefined) return "unknown age";
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

  <label class="sr-only" for="target-select">Target</label>
  <div class="relative min-w-0">
    <select
      id="target-select"
      class="h-7 w-[20rem] max-w-[26vw] min-w-[11rem] appearance-none rounded-control border border-line bg-raised px-2 pr-6 text-fine text-text"
      value={selectedId}
      title={selectedLabel ? `${selectedLabel} — ${selectedId}` : "Select a target"}
      onchange={(event) => onTarget(event.currentTarget.value)}
    >
      {#if liveActive}
        <option value="live">Live sky position</option>
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
    {#if liveEnabled}
      <button
        type="button"
        class={button}
        aria-haspopup="dialog"
        title="Fetch a timeline for any sky position from the live archive."
        onclick={onLive}
      >
        <svg
          viewBox="0 0 12 12"
          class="h-3 w-3 fill-none stroke-signal [stroke-width:1.4]"
          aria-hidden="true"
        >
          <circle cx="6" cy="6" r="1.6" class="fill-signal stroke-none" />
          <path d="M6 0.8v2M6 9.2v2M0.8 6h2M9.2 6h2" />
        </svg>
        Live
      </button>
    {/if}
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
