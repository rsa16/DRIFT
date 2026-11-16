<script lang="ts">
  import type { WindowInfo } from "$lib/api";
  import type { RenderPreset } from "$lib/PlaneRenderer";
  import { stretch, type HistogramData } from "$lib/histogram";

  interface Props {
    data: HistogramData | null;
    window: WindowInfo | null;
    tone: RenderPreset;
  }

  let { data, window: stretchWindow, tone }: Props = $props();

  let canvasEl = $state<HTMLCanvasElement>();
  let hostEl = $state<HTMLDivElement>();

  function draw(): void {
    const canvas = canvasEl;
    if (!canvas) return;
    const width = hostEl?.clientWidth ?? 0;
    const height = 74;
    if (!width) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const padX = 2;
    const plotW = width - padX * 2;
    const baseY = height - 15;
    const plotH = baseY - 4;

    // grid
    ctx.strokeStyle = "rgba(255,255,255,0.055)";
    ctx.lineWidth = 1;
    for (const frac of [0.25, 0.5, 0.75]) {
      const y = baseY - plotH * frac;
      ctx.beginPath();
      ctx.moveTo(padX, y + 0.5);
      ctx.lineTo(width - padX, y + 0.5);
      ctx.stroke();
    }
    ctx.strokeStyle = "rgba(255,255,255,0.1)";
    ctx.beginPath();
    ctx.moveTo(padX, baseY + 0.5);
    ctx.lineTo(width - padX, baseY + 0.5);
    ctx.stroke();

    if (!data || !stretchWindow) {
      ctx.fillStyle = "rgba(163,168,177,0.55)";
      ctx.font = "11px 'IBM Plex Sans', system-ui, sans-serif";
      ctx.fillText("No image loaded", padX + 4, baseY - 8);
      return;
    }

    const binW = plotW / data.bins.length;
    data.bins.forEach((count, index) => {
      const x = padX + index * binW;
      const barH = Math.max(1, count * plotH * 0.94);
      const mid = ((index + 0.5) / data.bins.length) * 100;
      const inside = mid >= data.loPct && mid <= data.hiPct;
      ctx.fillStyle = inside
        ? "rgba(138,169,198,0.5)"
        : "rgba(163,168,177,0.22)";
      ctx.fillRect(x + 0.4, baseY - barH, Math.max(binW - 0.8, 0.6), barH);
    });

    // stretch transfer curve, sampled across the domain
    ctx.beginPath();
    ctx.strokeStyle = "rgba(201,160,106,0.9)";
    ctx.lineWidth = 1.25;
    for (let index = 0; index <= 72; index += 1) {
      const t = index / 72;
      const value = data.domainLo + t * (data.domainHi - data.domainLo);
      const mapped = stretch(value, stretchWindow, tone);
      const x = padX + t * plotW;
      const y = baseY - mapped * plotH;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // window edges
    for (const pct of [data.loPct, data.hiPct]) {
      if (pct < 0 || pct > 100) continue;
      const x = padX + (pct / 100) * plotW;
      ctx.strokeStyle = "rgba(201,160,106,0.55)";
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(x + 0.5, 2);
      ctx.lineTo(x + 0.5, baseY);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  function observe(node: HTMLElement) {
    const observer = new ResizeObserver(() => draw());
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
  }

  $effect(() => {
    void data;
    void tone;
    void stretchWindow;
    draw();
  });
</script>

<div class="flex flex-col gap-2" data-testid="stretch-preview">
  <div
    bind:this={hostEl}
    use:observe
    class="relative overflow-hidden rounded-[3px] border border-line bg-void"
    role="img"
    aria-label={`Tone preview: ${tone} transfer curve with window lo ${stretchWindow?.lo ?? "—"} and hi ${stretchWindow?.hi ?? "—"}`}
  >
    <canvas
      bind:this={canvasEl}
      class="block h-[74px] w-full"
      aria-hidden="true"
    ></canvas>
  </div>
  <dl
    class="grid grid-cols-3 gap-2 border-t border-line pt-2 text-fine text-muted"
    aria-label="Stretch window"
  >
    <div>
      <dt class="text-micro text-faint">lo</dt>
      <dd class="m-0 truncate" title={String(stretchWindow?.lo ?? "—")}>
        {stretchWindow ? stretchWindow.lo.toPrecision(4) : "—"}
      </dd>
    </div>
    <div>
      <dt class="text-micro text-faint">hi</dt>
      <dd class="m-0 truncate" title={String(stretchWindow?.hi ?? "—")}>
        {stretchWindow ? stretchWindow.hi.toPrecision(4) : "—"}
      </dd>
    </div>
    <div>
      <dt class="text-micro text-faint">s</dt>
      <dd class="m-0 truncate" title={String(stretchWindow?.s ?? "—")}>
        {stretchWindow ? stretchWindow.s.toPrecision(3) : "—"}
      </dd>
    </div>
  </dl>
</div>
