<script lang="ts">
  interface Props {
    state: string;
    title: string;
    message: string;
    onRetry?: () => void;
  }

  let { state, title, message, onRetry }: Props = $props();
</script>

<div
  class="w-full max-w-sm rounded-panel border border-line bg-surface px-4 py-3"
  data-state={state}
  role={state === "error" ? "alert" : "status"}
  aria-live="polite"
>
  <h3 class="m-0 text-base font-medium text-text">{title}</h3>
  <p class="mt-1 mb-2 text-body leading-relaxed text-muted">{message}</p>
  {#if onRetry}
    <button
      type="button"
      class="rounded-control border border-line bg-raised px-2 py-1 text-label text-text transition-colors hover:bg-hover"
      onclick={onRetry}
    >
      Retry
    </button>
  {/if}
</div>
