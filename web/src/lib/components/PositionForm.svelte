<script lang="ts">
  interface Props {
    ra: number;
    dec: number;
    fov: number;
    maxFov: number;
    busy: boolean;
    notice?: string;
    submitLabel: string;
    resetKey?: unknown;
    onSubmit: (ra: number, dec: number, fov: number, bins: number) => void;
  }

  let {
    ra,
    dec,
    fov,
    maxFov,
    busy,
    notice = "",
    submitLabel,
    resetKey,
    onSubmit,
  }: Props = $props();

  let raValue = $state("");
  let decValue = $state("");
  let fovValue = $state("");
  let bins = $state(8);
  let localError = $state("");

  $effect(() => {
    resetKey;
    raValue = String(ra);
    decValue = String(dec);
    fovValue = String(fov);
    localError = "";
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

<form
  class="flex flex-col gap-3 px-4 py-4"
  data-testid="coordinate-form"
  onsubmit={submit}
>
  <div class="grid grid-cols-2 gap-3">
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

  {#if localError || notice}
    <p class="m-0 text-fine text-warm" role="status">
      {localError || notice}
    </p>
  {/if}

  <div class="flex justify-end">
    <button
      type="submit"
      class="rounded-control border border-signal/60 bg-signal/15 px-3 py-1 text-label text-signal transition-colors hover:bg-signal/25 disabled:cursor-not-allowed disabled:opacity-40"
      disabled={busy}
    >
      {busy ? "Resolving…" : submitLabel}
    </button>
  </div>
</form>
