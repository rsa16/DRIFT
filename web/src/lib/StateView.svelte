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
  class="state"
  data-state={state}
  role={state === "error" ? "alert" : "status"}
  aria-live="polite"
>
  <h3>{title}</h3>
  <p>{message}</p>
  {#if onRetry}
    <button type="button" onclick={onRetry}>Retry</button>
  {/if}
</div>
