<script lang="ts">
  interface Props {
    open: boolean;
    onClose: () => void;
  }

  let { open, onClose }: Props = $props();

  let dialog = $state<HTMLDialogElement>();

  const key =
    "rounded-[2px] border border-line bg-raised px-1 py-px font-mono text-micro text-text";

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  });

  const terms: { term: string; text: string }[] = [
    {
      term: "Visit",
      text: "One SPHEREx exposure of this part of the sky. The timeline starts with the oldest visit.",
    },
    {
      term: "Visit stack",
      text: "All the visits in the same band, shown with the same brightness range and units.",
    },
    {
      term: "Band · matched λ",
      text: "The wavelength range. Images are grouped together only when their measured wavelengths are close enough. Changing the band loads that band’s images.",
    },
    {
      term: "Window",
      text: "The brightness range used for every image in the stack. Using one range makes it easier to compare visits. The window hash identifies that range.",
    },
    {
      term: "Tone",
      text: "How brightness is mapped on screen: linear, asinh or log. Asinh is the default.",
    },
    {
      term: "Difference",
      text: "Subtracts the reference visit from the one you’re viewing. Unchanged areas disappear, while changes stand out. Blue means brighter now, amber means dimmer, and magenta means the data is missing.",
    },
    {
      term: "Reference (REF)",
      text: "The visit used for the Difference view. It starts as the first visit, unless you choose another one.",
    },
    {
      term: "Survey compare",
      text: "Two surveys from different times, matched at the same band, so you can compare the same patch of sky.",
    },
    {
      term: "DR1 · QR3",
      text: "Data releases. DR1 is the reprocessed release. QR3 is a quick, lightly checked release that will eventually be replaced.",
    },
    {
      term: "MJD",
      text: "Modified Julian Date: the observation time, measured in days.",
    },
    {
      term: "Detector",
      text: "The part of the SPHEREx camera that saw this position. It determines the wavelength, and therefore the band for that visit.",
    },
    {
      term: "Pixel scale · FOV",
      text: "The size of each pixel and the width of the image, measured across the 512-pixel field.",
    },
    {
      term: "Snapshot",
      text: "The fixed set of processed data used by this page. A Fixture snapshot is made-up test data, marked in the header, not data from the archive.",
    },
  ];
</script>

<dialog
  bind:this={dialog}
  class="m-auto w-[min(44rem,calc(100vw-2rem))] rounded-panel border border-line bg-surface p-0 text-text backdrop:bg-void/70"
  aria-labelledby="help-title"
  onclose={onClose}
>
  <div class="flex items-center justify-between border-b border-line px-4 py-3">
    <h2 id="help-title" class="m-0 text-title font-medium">How to read Drift</h2>
    <button
      type="button"
      class="rounded-control border border-line bg-raised px-2 py-1 text-label text-muted transition-colors hover:bg-hover hover:text-text"
      onclick={() => dialog?.close()}
    >
      Close
    </button>
  </div>

  <div class="max-h-[75vh] overflow-y-auto px-4 py-3 text-label leading-relaxed text-muted">
    <p class="m-0 mb-3">
      Drift plays through a stack of SPHEREx images of the same "visits" of the sky. Every
      image uses the same brightness range, pixel scale and units.
    </p>

    <h3 class="m-0 mb-2 text-fine font-medium tracking-[0.12em] uppercase text-faint">
      Terms
    </h3>
    <dl class="m-0 mb-4 flex flex-col gap-2">
      {#each terms as entry}
        <div class="grid grid-cols-[9rem_minmax(0,1fr)] gap-3">
          <dt class="m-0 text-text">{entry.term}</dt>
          <dd class="m-0">{entry.text}</dd>
        </div>
      {/each}
    </dl>

    <h3 class="m-0 mb-2 text-fine font-medium tracking-[0.12em] uppercase text-faint">
      Controls
    </h3>
    <ul class="m-0 mb-4 flex list-none flex-col gap-1.5 p-0">
      <li><kbd class={key}>Space</kbd> Play or pause the stack.</li>
      <li><kbd class={key}>←</kbd> <kbd class={key}>→</kbd> Move to the previous or next visit.</li>
      <li>Click a tick in the timeline to jump to that visit.</li>
      <li><kbd class={key}>REF</kbd> Choose the visit used for the Difference view.</li>
      <li><kbd class={key}>Share</kbd> Copy a link to this target, band and visit.</li>
    </ul>

    <h3 class="m-0 mb-2 text-fine font-medium tracking-[0.12em] uppercase text-faint">
      Reading the frame
    </h3>
    <p class="m-0">
      The caption under the image shows the visit date, MJD, data release and visit
      ID. The side panel shows the detector, wavelength range, pixel scale, units and
      window hash used for the whole stack. Notes about the observation and any
      images that could not be loaded appear in small grey text in the corner.
    </p>
  </div>
</dialog>
