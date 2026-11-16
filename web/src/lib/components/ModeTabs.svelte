<script lang="ts">
  interface ModeRow {
    key: string;
    label: string;
    enabled: boolean;
    reason: string;
  }

  interface Props {
    modes: ModeRow[];
    stage: "timeline" | "compare";
    onStage: (stage: "timeline" | "compare") => void;
  }

  let { modes, stage, onStage }: Props = $props();

  const modeByName = $derived(
    Object.fromEntries(modes.map((mode) => [mode.key, mode])),
  );

  interface Tab {
    value: "timeline" | "compare" | "spectrum";
    key: string;
    label: string;
    help: string;
  }

  const tabs: Tab[] = [
    {
      value: "timeline",
      key: "timeline",
      label: "Timeline",
      help: "Blink between matched-band visits.",
    },
    {
      value: "compare",
      key: "survey_compare",
      label: "Compare",
      help: "Compare matched bands across survey epochs.",
    },
    {
      value: "spectrum",
      key: "spectrum",
      label: "Spectrum",
      help: "Read the cube spectrum and its provenance.",
    },
  ];

  const current = $derived(stage === "compare" ? "compare" : "timeline");
  let hovered = $state<string | null>(null);

  const selectable = (value: Tab["value"]): boolean => {
    if (value === "spectrum") return false;
    const tab = tabs.find((item) => item.value === value);
    return tab ? modeByName[tab.key]?.enabled === true : false;
  };
  const refused = $derived(
    tabs
      .filter((tab) => !selectable(tab.value))
      .map((tab) => ({
        label: tab.label,
        reason:
          modeByName[tab.key]?.reason ||
          `${tab.label} is unavailable in this build.`,
      })),
  );

  function select(value: Tab["value"]): void {
    if (!selectable(value)) return;
    if (value === "timeline" || value === "compare") onStage(value);
  }
</script>

<div class="flex flex-col gap-1.5">
  <div
    class="inline-flex w-full items-center rounded-control border border-line bg-void p-[2px]"
    role="group"
    aria-label="Observation mode"
    data-testid="mode-switcher"
  >
    {#each tabs as tab (tab.value)}
      {@const mode = modeByName[tab.key]}
      {@const enabled = selectable(tab.value)}
      <span
        class="relative flex-1"
        role="presentation"
        onmouseenter={() => (hovered = tab.value)}
        onmouseleave={() => {
          if (hovered === tab.value) hovered = null;
        }}
      >
        <button
          type="button"
          class="w-full rounded-[2px] px-2 py-[6px] text-label leading-none transition-colors {current ===
          tab.value
            ? 'bg-raised text-text'
            : 'text-muted hover:text-text'} {enabled
            ? ''
            : 'cursor-not-allowed text-faint opacity-45'}"
          aria-pressed={current === tab.value}
          aria-disabled={!enabled}
          aria-label={enabled ? undefined : `${tab.label}: unavailable`}
          title={enabled ? tab.help : undefined}
          disabled={!enabled}
          data-mode={tab.value}
          onclick={() => select(tab.value)}
        >
          {tab.label}
        </button>
        {#if !enabled && hovered === tab.value}
          <span
            class="pointer-events-none absolute top-full z-30 mt-2 w-60 rounded-[3px] border border-line bg-raised px-2 py-1.5 text-left text-fine leading-snug text-muted {tab.value ===
            'compare'
              ? 'left-1/2 -translate-x-1/2'
              : tab.value === 'timeline'
                ? 'left-0'
                : 'right-0'}"
            role="tooltip"
          >
            {mode?.reason || `${tab.label} is unavailable in this build.`}
          </span>
        {/if}
      </span>
    {/each}
  </div>

  {#if refused.length}
    <ul
      class="m-0 flex list-none flex-col gap-1 p-0 text-fine leading-snug text-muted"
      data-testid="mode-reasons"
    >
      {#each refused as item (item.label)}
        <li data-mode-reason={item.label}>
          <span class="text-faint">{item.label} unavailable —</span>
          {item.reason}
        </li>
      {/each}
    </ul>
  {/if}
</div>
