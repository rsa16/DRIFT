<script lang="ts">
  import PositionForm from "$lib/components/PositionForm.svelte";

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

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      const field = dialog.querySelector<HTMLInputElement>("input");
      field?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });
</script>

<dialog
  bind:this={dialog}
  class="m-auto w-[min(26rem,calc(100vw-2rem))] rounded-panel border border-line bg-surface p-0 text-text backdrop:bg-void/70"
  aria-labelledby="position-title"
  onclose={onClose}
>
  <div class="flex items-center justify-between border-b border-line px-4 py-3">
    <div>
      <h2 id="position-title" class="m-0 text-title font-medium">Sky coordinates</h2>
      <p class="m-0 mt-0.5 text-micro text-faint">
        Change the position or the field of view.
      </p>
    </div>
    <button
      type="button"
      class="rounded-control border border-line bg-raised px-2 py-1 text-label text-muted transition-colors hover:bg-hover hover:text-text"
      onclick={() => dialog?.close()}
    >
      Close
    </button>
  </div>

  <PositionForm
    {ra}
    {dec}
    {fov}
    {maxFov}
    {busy}
    {notice}
    submitLabel="View"
    resetKey={open}
    {onSubmit}
  />
</dialog>
