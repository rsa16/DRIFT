import type { WindowInfo } from "./api";
import type { RenderPreset } from "./PlaneRenderer";
import type { DecodedPlane } from "./spx1.ts";

/** Decode one IEEE-754 half precision value from its 16 raw bits. */
export function halfToNumber(bits: number): number {
  const sign = bits & 0x8000 ? -1 : 1;
  const exponent = (bits >> 10) & 0x1f;
  const fraction = bits & 0x3ff;
  if (exponent === 0) return sign * fraction * 2 ** -24;
  if (exponent === 31) return fraction ? Number.NaN : sign * Infinity;
  return sign * (1 + fraction / 1024) * 2 ** (exponent - 15);
}

export interface HistogramData {
  bins: number[];
  domainLo: number;
  domainHi: number;
  loPct: number;
  hiPct: number;
  samples: number;
}

const SAMPLE_TARGET = 8192;
const BIN_COUNT = 56;

/**
 * Build a robust histogram for a plane, sampled sparsely so it can be
 * recomputed on every frame without stalling playback.
 */
export function buildHistogram(
  plane: DecodedPlane | null,
  window: WindowInfo | null,
): HistogramData | null {
  if (!plane || !window) return null;
  const total = plane.data.length;
  if (!total) return null;
  const stride = Math.max(1, Math.floor(total / SAMPLE_TARGET));
  const values: number[] = [];
  for (let index = 0; index < total; index += stride) {
    const value = halfToNumber(plane.data[index]);
    if (Number.isFinite(value)) values.push(value);
  }
  if (values.length < 8) return null;
  values.sort((a, b) => a - b);
  const low = values[Math.floor(values.length * 0.02)];
  const high = values[Math.floor(values.length * 0.98)];
  const spread = Math.max(high - low, 1e-6);
  const domainLo = Math.min(low - spread * 0.05, window.lo);
  const domainHi = Math.max(high + spread * 0.05, window.hi);
  const span = Math.max(domainHi - domainLo, 1e-6);
  const bins = new Array<number>(BIN_COUNT).fill(0);
  for (const value of values) {
    const t = (value - domainLo) / span;
    const bin = Math.min(
      BIN_COUNT - 1,
      Math.max(0, Math.floor(t * BIN_COUNT)),
    );
    bins[bin] += 1;
  }
  const peak = Math.max(...bins, 1);
  return {
    bins: bins.map((count) => count / peak),
    domainLo,
    domainHi,
    loPct: ((window.lo - domainLo) / span) * 100,
    hiPct: ((window.hi - domainLo) / span) * 100,
    samples: values.length,
  };
}

/** Same transfer curve as the WebGL renderer, for the synced preview. */
export function stretch(
  value: number,
  window: WindowInfo,
  tone: RenderPreset,
): number {
  const span = Math.max(window.hi - window.lo, 1e-6);
  const normalized = Math.min(
    1,
    Math.max(0, (value - window.lo) / span),
  );
  if (tone === "asinh") {
    const sigma = Math.max(window.s, 1e-6);
    const denominator = Math.max(Math.asinh(span / sigma), 1e-6);
    return Math.min(
      1,
      Math.max(0, Math.asinh(Math.max(value - window.lo, 0) / sigma) / denominator),
    );
  }
  if (tone === "log") {
    return Math.min(1, Math.max(0, Math.log1p(9 * normalized) / Math.log(10)));
  }
  return normalized;
}
