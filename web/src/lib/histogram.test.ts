import assert from "node:assert/strict";
import test from "node:test";

import type { DecodedPlane } from "./spx1.ts";
import { buildHistogram, halfToNumber, stretch } from "./histogram.ts";

function toHalf(value: number): number {
  const floats = new Float32Array(1);
  const words = new Uint32Array(floats.buffer);
  floats[0] = value;
  const bits = words[0];
  const sign = (bits >>> 16) & 0x8000;
  const exponent = ((bits >>> 23) & 0xff) - 127 + 15;
  const fraction = bits & 0x7fffff;
  if (exponent <= 0) return sign;
  return sign | (exponent << 10) | (fraction >>> 13);
}

function planeFromValues(values: number[]): DecodedPlane {
  const data = new Uint16Array(values.length);
  values.forEach((value, index) => {
    data[index] = toHalf(value);
  });
  return {
    header: {
      magic: "SPX1",
      version: 1,
      dtype: 1,
      channels: 1,
      flags: 0,
      height: 1,
      width: values.length,
      scale: 1,
      offset: 0,
      crc32: 0,
      frameIndex: 0,
      reserved: 0,
    },
    width: values.length,
    height: 1,
    data,
  };
}

test("decodes IEEE half precision including subnormals and specials", () => {
  assert.equal(halfToNumber(0x3c00), 1);
  assert.equal(halfToNumber(0xc000), -2);
  assert.equal(halfToNumber(0x0000), 0);
  assert.equal(halfToNumber(0x0001), 2 ** -24);
  assert.equal(halfToNumber(0x7c00), Infinity);
  assert.equal(halfToNumber(0xfc00), -Infinity);
  assert.ok(Number.isNaN(halfToNumber(0x7e00)));
});

test("histogram spans the data and keeps the window inside the domain", () => {
  const values = Array.from({ length: 256 }, (_, index) => index / 16);
  const histogram = buildHistogram(planeFromValues(values), {
    lo: 4,
    hi: 12,
    s: 1,
    sha256: "x",
  });
  assert.ok(histogram);
  assert.equal(histogram.bins.length, 56);
  assert.ok(histogram.domainLo <= 4);
  assert.ok(histogram.domainHi >= 12);
  assert.ok(histogram.loPct >= 0 && histogram.loPct <= 100);
  assert.ok(histogram.hiPct >= histogram.loPct);
  assert.ok(Math.max(...histogram.bins) <= 1);
  assert.equal(buildHistogram(null, null), null);
});

test("the preview stretch matches the renderer presets", () => {
  const window = { lo: 0, hi: 10, s: 2, sha256: "x" };
  assert.equal(stretch(5, window, "linear"), 0.5);
  assert.equal(stretch(-2, window, "linear"), 0);
  assert.equal(stretch(20, window, "linear"), 1);
  const asinh = stretch(5, window, "asinh");
  const log = stretch(5, window, "log");
  assert.ok(asinh > 0 && asinh < 1);
  assert.ok(log > 0 && log < 1);
  assert.ok(log > asinh, "log lifts the midtones more than asinh here");
});
