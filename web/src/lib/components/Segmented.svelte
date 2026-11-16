<script lang="ts">
  interface Option {
    value: string;
    label: string;
    disabled?: boolean;
    title?: string;
  }

  interface Props {
    options: Option[];
    value: string;
    label: string;
    onSelect: (value: string) => void;
    compact?: boolean;
    testId?: string;
  }

  let {
    options,
    value,
    label,
    onSelect,
    compact = false,
    testId,
  }: Props = $props();

  let hovered = $state<string | null>(null);
</script>

<div
  class="relative inline-flex items-center rounded-control border border-line bg-void p-[2px]"
  role="group"
  aria-label={label}
  data-testid={testId}
>
  {#each options as option (option.value)}
    <span
      class="group relative flex {option.disabled ? 'cursor-not-allowed' : ''}"
      role="presentation"
      onmouseenter={() => (hovered = option.value)}
      onmouseleave={() => {
        if (hovered === option.value) hovered = null;
      }}
    >
      <button
        type="button"
        class="relative rounded-[2px] leading-none transition-colors {compact
          ? 'px-1.5 py-[5px] text-fine'
          : 'px-2.5 py-[6px] text-label'} {option.value === value
          ? 'bg-raised text-text'
          : 'text-muted hover:text-text'} {option.disabled
          ? 'pointer-events-none cursor-not-allowed text-faint opacity-45'
          : ''}"
        aria-pressed={option.value === value}
        aria-label={option.disabled ? `${option.label}: unavailable` : undefined}
        disabled={option.disabled}
        title={option.disabled ? undefined : option.title}
        onclick={() => {
          if (option.disabled) return;
          onSelect(option.value);
        }}
      >
        {option.label}
      </button>
      {#if option.disabled && option.title && hovered === option.value}
        <span
          class="pointer-events-none absolute right-0 top-full z-30 mt-2 w-60 rounded-[3px] border border-line bg-raised px-2 py-1.5 text-left text-fine leading-snug text-muted"
          role="tooltip"
        >
          {option.title}
        </span>
      {/if}
    </span>
  {/each}
</div>
