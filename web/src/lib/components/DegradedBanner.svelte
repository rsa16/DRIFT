<script lang="ts">
  interface Props {
    degraded: boolean;
    omitted: string[];
    shown: number;
    available: number;
    unit: "visit" | "band" | "epoch";
    resolutionReduced: boolean;
    compressedBytes: number | null;
  }

  let {
    degraded,
    omitted,
    shown,
    available,
    unit,
    resolutionReduced,
    compressedBytes,
  }: Props = $props();

  const reason = $derived(
    omitted.length
      ? unit === "band" || unit === "epoch"
        ? `Some images were omitted. Showing ${shown}.`
        : `${shown} of ${available} visits; omitted ${omitted.join(", ")}.`
      : resolutionReduced
        ? "The response was too large, so fewer images are shown at lower resolution."
        : "The response was too large, so fewer images are shown.",
  );
</script>

{#if degraded}
  <div
    class="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-warn/40 bg-warn/10 px-3 py-1.5 text-label text-warn"
    role="status"
    data-testid="degraded-banner"
  >
    <span class="font-medium">Degraded</span>
    <span class="text-warn/90">{reason}</span>
    {#if compressedBytes}
      <span class="text-fine text-warn/70">
        {(compressedBytes / (1024 * 1024)).toFixed(1)} MiB compressed
      </span>
    {/if}
  </div>
{/if}
