<script lang="ts">
  interface Props {
    label: string;
    text: string;
  }

  let { label, text }: Props = $props();

  let open = $state(false);
</script>

<span
  class="relative inline-flex align-middle"
  role="presentation"
  onmouseenter={() => (open = true)}
  onmouseleave={() => (open = false)}
>
  <button
    type="button"
    class="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-line-strong/70 font-mono text-micro leading-none text-faint transition-colors hover:border-signal/60 hover:text-signal focus-visible:border-signal focus-visible:text-signal"
    aria-label={`${label}: ${text}`}
    aria-expanded={open}
    onclick={() => (open = !open)}
    onfocus={() => (open = true)}
    onblur={() => (open = false)}
  >
    i
  </button>
  {#if open}
    <span
      class="pointer-events-none absolute left-0 top-full z-30 mt-2 w-60 rounded-[3px] border border-line bg-raised px-2 py-1.5 text-left text-fine leading-snug text-muted"
      role="tooltip"
    >
      {text}
    </span>
  {/if}
</span>
