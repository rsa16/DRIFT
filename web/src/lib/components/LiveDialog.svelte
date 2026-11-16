<script lang="ts">
  interface Props {
    open: boolean;
    ra: number;
    dec: number;
    fov: number;
    maxFov: number;
    busy: boolean;
    notice: string;
    onClose: () => void;
    onSubmit: (ra: number, dec: number, fov: number, bins: number) => void;
  }

  let {
    open,
    ra,
    dec,
    fov,
    maxFov,
    busy,
    notice,
    onClose,
    onSubmit,
  }: Props = $props();

  let dialog = $state<HTMLDialogElement>();
  let raValue = $state("");
  let decValue = $state("");
  let fovValue = $state("");
  let bins = $state(8);
  let localError = $state("");

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      raValue = String(ra);
      decValue = String(dec);
      fovValue = String(fov);
      localError = "";
      dialog.showModal();
      const field = dialog.querySelector<HTMLInputElement>("input");
      field?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  const field =
    "h-8 w-full rounded-control border border-line bg-raised px-2 font-mono text-fine tabular-nums text-text";
  const label = "text-fine text-faint";

  function numberFrom(value: string): number | null {
    const trimmed = value.trim();
    if (trimmed === "") return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const raNumber = numberFrom(raValue);
    const decNumber = numberFrom(decValue);
    const fovNumber = numberFrom(fovValue);
    if (raNumber === null || raNumber < 0 || raNumber > 360) {
      localError = "Right ascension must be between 0 and 360°.";
      return;
    }
    if (decNumber === null || decNumber < -90 || decNumber > 90) {
      localError = "Declination must be between −90 and 90°.";
      return;
    }
    if (fovNumber === null || fovNumber <= 0 || fovNumber > maxFov) {
      localError = `Field of view must be greater than 0 and at most ${maxFov}°.`;
      return;
    }
    localError = "";
    onSubmit(raNumber, decNumber, fovNumber, bins);
  }
</script>

<dialog
  bind:this={dialog}
  class="m-auto w-[min(26rem,calc(100vw-2rem))] rounded-panel border border-line bg-surface p-0 text-text backdrop:bg-void/70"
  aria-labelledby="live-title"
  onclose={onClose}
>
  <form class="flex flex-col" onsubmit={submit}>
    <div class="flex items-center justify-between border-b border-line px-4 py-3">
      <h2 id="live-title" class="m-0 text-title font-medium">Live sky position</h2>
      <button
        type="button"
        class="rounded-control border border-line bg-raised px-2 py-1 text-label text-muted transition-colors hover:bg-hover hover:text-text"
        onclick={() => dialog?.close()}
      >
        Close
      </button>
    </div>

    <div class="grid grid-cols-2 gap-3 px-4 py-4" data-testid="live-form">
      <label class="flex flex-col gap-1">
        <span class={label}>Right ascension (°)</span>
        <input
          class={field}
          name="ra"
          inputmode="decimal"
          autocomplete="off"
          bind:value={raValue}
          disabled={busy}
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class={label}>Declination (°)</span>
        <input
          class={field}
          name="dec"
          inputmode="decimal"
          autocomplete="off"
          bind:value={decValue}
          disabled={busy}
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class={label}>Field of view (°)</span>
        <input
          class={field}
          name="fov"
          inputmode="decimal"
          autocomplete="off"
          bind:value={fovValue}
          disabled={busy}
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class={label}>Frames</span>
        <select class={field} name="bins" bind:value={bins} disabled={busy}>
          <option value={2}>2</option>
          <option value={4}>4</option>
          <option value={8}>8</option>
          <option value={16}>16</option>
        </select>
      </label>
    </div>

    <p class="m-0 px-4 text-micro leading-relaxed text-faint">
      Frames are fetched from IRSA for this position. A live timeline is limited to
      six requests per minute and 30 seconds; a pinned target is unaffected.
    </p>

    {#if localError || notice}
      <p class="m-0 px-4 pt-2 text-fine text-warm" role="status">
        {localError || notice}
      </p>
    {/if}

    <div class="flex justify-end gap-2 border-t border-line px-4 py-3">
      <button
        type="submit"
        class="rounded-control border border-signal/60 bg-signal/15 px-3 py-1 text-label text-signal transition-colors hover:bg-signal/25 disabled:cursor-not-allowed disabled:opacity-40"
        disabled={busy}
      >
        {busy ? "Resolving…" : "Show timeline"}
      </button>
    </div>
  </form>
</dialog>
