export interface FrameFacts {
  visitId: string | null;
  epoch: string | null;
  date: string | null;
  mjd: number | null;
  release: string;
}

export interface FrameMetaLike {
  visit_id?: string;
  epoch_id?: string;
  date?: string | null;
  mjd?: number | null;
  mjd_min?: number | null;
  release: string;
}

export interface StackFacts {
  detector: string | null;
  lambdaLoUm: number;
  lambdaHiUm: number;
  pixelScaleArcsec: number;
  units: string;
  windowSha: string;
}

export interface ReadoutRow {
  label: string;
  value: string;
  detail?: string;
}

export interface ScaleBar {
  arcsec: number;
  pixels: number;
  label: string;
}

const NICE_ARCSEC = [
  1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 3600,
];

export function formatDate(value: string | null | undefined): string {
  if (!value) return "date unknown";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return `${parsed.toISOString().slice(0, 16).replace("T", " ")}Z`;
}

export function formatMjd(value: number | null | undefined): string {
  return value === null || value === undefined ? "MJD —" : `MJD ${value.toFixed(3)}`;
}

export function formatLambda(loUm: number, hiUm: number): string {
  return `${loUm.toFixed(2)}–${hiUm.toFixed(2)} µm`;
}

export function formatScale(pixelScaleArcsec: number): string {
  return `${pixelScaleArcsec.toFixed(3)}″/px`;
}

export function releaseLabel(release: string | null | undefined): string {
  return release ? release.toUpperCase() : "—";
}

export function visitLabel(frame: FrameFacts | null | undefined): string {
  if (!frame) return "—";
  return frame.visitId?.trim() ? frame.visitId : "—";
}

export function frameFacts(
  meta: FrameMetaLike | null | undefined,
): FrameFacts | null {
  if (!meta) return null;
  return {
    visitId: meta.visit_id ?? meta.epoch_id ?? null,
    epoch: meta.epoch_id ?? null,
    date: meta.date ?? null,
    mjd: meta.mjd ?? meta.mjd_min ?? null,
    release: meta.release,
  };
}

export function stepAccessibleName(
  frame: FrameFacts | null,
  position: number,
): string {
  if (!frame) return `Step ${position + 1}`;
  const epoch = frame.epoch ? `, epoch ${frame.epoch}` : "";
  return (
    `Step ${position + 1}: ${visitLabel(frame)}, ${formatDate(frame.date)}, ` +
    `${formatMjd(frame.mjd)}, ${releaseLabel(frame.release)}${epoch}`
  );
}

export function stepShortLabel(
  frame: FrameFacts | null,
  compare: boolean,
): string {
  if (!frame) return "—";
  if (compare) return releaseLabel(frame.release);
  return frame.date ? frame.date.slice(0, 10) : "—";
}

export interface CadenceStep {
  release: string;
  mjd: number | null;
}

export interface EpochGroup {
  release: string;
  start: number;
  end: number;
  gapDays: number | null;
  gapFromPct: number;
  gapToPct: number;
}

/**
 * Lay visits out on a cadence track in percent units. Consecutive gaps are
 * proportional to elapsed days but capped, so a multi-month epoch break reads
 * as a clear divider instead of stretching the track into empty space.
 */
export function cadencePositions(steps: CadenceStep[]): number[] {
  const count = steps.length;
  if (count === 0) return [];
  if (count === 1) return [50];
  const mjds = steps.map((step) => step.mjd);
  const finite = mjds.filter(
    (mjd): mjd is number => mjd !== null && Number.isFinite(mjd),
  );
  if (finite.length < count) {
    return steps.map((_, index) => (index / (count - 1)) * 100);
  }
  const deltas: number[] = [];
  for (let index = 1; index < count; index += 1) {
    const delta = (mjds[index] as number) - (mjds[index - 1] as number);
    if (delta > 0) deltas.push(delta);
  }
  const sorted = [...deltas].sort((a, b) => a - b);
  const median = sorted.length
    ? sorted[Math.floor(sorted.length / 2)]
    : 1;
  const cap = Math.max(median * 3, 0.25);
  const weights: number[] = [];
  for (let index = 1; index < count; index += 1) {
    const delta = (mjds[index] as number) - (mjds[index - 1] as number);
    weights.push(delta > 0 ? Math.min(delta, cap) : cap * 0.2);
  }
  const total = weights.reduce((sum, weight) => sum + weight, 0) || 1;
  const positions = [0];
  let travelled = 0;
  for (const weight of weights) {
    travelled += weight;
    positions.push((travelled / total) * 100);
  }
  return positions;
}

/** Group consecutive visits by release and describe the break between groups. */
export function epochGroups(
  steps: CadenceStep[],
  positions: number[],
): EpochGroup[] {
  const groups: EpochGroup[] = [];
  for (let index = 0; index < steps.length; index += 1) {
    const release = releaseLabel(steps[index].release);
    const last = groups[groups.length - 1];
    if (last && last.release === release) {
      last.end = index;
      continue;
    }
    groups.push({
      release,
      start: index,
      end: index,
      gapDays: null,
      gapFromPct: positions[index] ?? 0,
      gapToPct: positions[index] ?? 0,
    });
  }
  for (let index = 1; index < groups.length; index += 1) {
    const previous = groups[index - 1];
    const group = groups[index];
    const before = steps[previous.end].mjd;
    const after = steps[group.start].mjd;
    group.gapDays =
      before !== null && after !== null && after > before
        ? Math.round(after - before)
        : null;
    group.gapFromPct = positions[previous.end] ?? 0;
    group.gapToPct = positions[group.start] ?? 100;
  }
  return groups;
}

export interface BandTick {
  index: number;
  band: number;
  lambdaLoUm: number;
  lambdaHiUm: number;
}

export interface BandMetaLike {
  band_index?: number;
  band: number;
  lambda_lo_um: number;
  lambda_hi_um: number;
}

export function bandTicks(planes: BandMetaLike[]): BandTick[] {
  const seen = new Set<number>();
  const ticks: BandTick[] = [];
  for (const plane of planes) {
    const index = plane.band_index ?? ticks.length;
    if (seen.has(index)) continue;
    seen.add(index);
    ticks.push({
      index,
      band: plane.band,
      lambdaLoUm: plane.lambda_lo_um,
      lambdaHiUm: plane.lambda_hi_um,
    });
  }
  return ticks;
}

export function captionFacts(frame: FrameFacts): string[] {
  return [
    formatDate(frame.date),
    formatMjd(frame.mjd),
    releaseLabel(frame.release),
    visitLabel(frame),
  ];
}

export function readoutRows(stack: StackFacts): ReadoutRow[] {
  const sha = stack.windowSha || "—";
  return [
    { label: "Detector", value: stack.detector ?? "—" },
    { label: "Matched λ", value: formatLambda(stack.lambdaLoUm, stack.lambdaHiUm) },
    { label: "Pixel scale", value: formatScale(stack.pixelScaleArcsec) },
    { label: "Units", value: stack.units || "—" },
    { label: "Window hash", value: sha === "—" ? sha : sha.slice(0, 12), detail: sha },
  ];
}

export function canvasAltText(frame: FrameFacts, stack: StackFacts): string {
  return (
    `Visit ${visitLabel(frame)} on ${formatDate(frame.date)}, ` +
    `${formatMjd(frame.mjd)}, release ${releaseLabel(frame.release)}. ` +
    `Detector ${stack.detector ?? "unknown"}, ` +
    `${formatLambda(stack.lambdaLoUm, stack.lambdaHiUm)}, ` +
    `${formatScale(stack.pixelScaleArcsec)}, ${stack.units || "unknown units"}.`
  );
}

export function chooseScaleBar(
  pixelScaleArcsec: number,
  planePx: number,
  canvasPx: number,
  availablePx: number,
): ScaleBar | null {
  if (pixelScaleArcsec <= 0 || planePx <= 0 || canvasPx <= 0) return null;
  if (availablePx < 40) return null;
  const arcsecPerCanvasPx = (pixelScaleArcsec * planePx) / canvasPx;
  const maxArcsec = availablePx * arcsecPerCanvasPx * 0.92;
  let chosen = 0;
  for (const candidate of NICE_ARCSEC) {
    if (candidate <= maxArcsec) chosen = candidate;
  }
  if (chosen === 0) return null;
  const pixels = chosen / arcsecPerCanvasPx;
  const label = chosen >= 60 ? `${Math.round(chosen / 60)}′` : `${chosen}″`;
  return { arcsec: chosen, pixels, label };
}
