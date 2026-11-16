<script lang="ts">
  interface Props {
    ra: number;
    dec: number;
    fov: number;
    label: string;
  }

  let { ra, dec, fov, label }: Props = $props();

  const x = $derived((((ra % 360) + 360) % 360) / 360 * 100);
  const y = $derived((90 - dec) / 180 * 50);
  const boxWidth = $derived(Math.max(1.4, fov / 360 * 100));
  const boxHeight = $derived(Math.max(1.4, fov / 180 * 50));
</script>

<svg
  class="block h-auto w-full rounded-control border border-line bg-void"
  viewBox="0 0 100 50"
  role="img"
  aria-label={label}
  data-testid="mini-map"
>
  <title>{label}</title>
  <rect class="fill-void" x="0" y="0" width="100" height="50" />
  {#each [25, 50, 75] as gridX}
    <line class="stroke-line [stroke-width:0.25]" x1={gridX} y1="0" x2={gridX} y2="50" />
  {/each}
  {#each [12.5, 25, 37.5] as gridY}
    <line class="stroke-line [stroke-width:0.25]" x1="0" y1={gridY} x2="100" y2={gridY} />
  {/each}
  <rect
    class="fill-none stroke-warm [stroke-width:0.45]"
    x={x - boxWidth / 2}
    y={y - boxHeight / 2}
    width={boxWidth}
    height={boxHeight}
  />
  <circle class="fill-signal stroke-text [stroke-width:0.25]" cx={x} cy={y} r="1.2" />
  <text class="fill-faint font-sans [font-size:3.4px]" x="2" y="6">RA {ra.toFixed(2)}°</text>
  <text class="fill-faint font-sans [font-size:3.4px]" x="2" y="46">Dec {dec.toFixed(2)}° · FOV {fov.toFixed(2)}°</text>
</svg>
