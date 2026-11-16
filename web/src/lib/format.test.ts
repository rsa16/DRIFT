import assert from "node:assert/strict";
import test from "node:test";

import {
  bandTicks,
  captionFacts,
  canvasAltText,
  chooseScaleBar,
  formatDate,
  formatLambda,
  formatMjd,
  formatScale,
  frameFacts,
  readoutRows,
  releaseLabel,
  cadencePositions,
  epochGroups,
  stepAccessibleName,
  stepShortLabel,
  visitLabel,
  type FrameFacts,
  type StackFacts,
} from "./format.ts";

const frame: FrameFacts = {
  visitId: "qr3-61273.38316-D5",
  epoch: null,
  date: "2026-08-21T09:11:00Z",
  mjd: 61273.383,
  release: "qr3",
};

const stack: StackFacts = {
  detector: "D5",
  lambdaLoUm: 3.82,
  lambdaHiUm: 3.9,
  pixelScaleArcsec: 6.158,
  units: "MJy/sr",
  windowSha: "36d12ef6e0b4aabbccdd",
};

test("formats visit date, MJD, wavelength, scale and release", () => {
  assert.equal(formatDate("2026-08-21T09:11:00Z"), "2026-08-21 09:11Z");
  assert.equal(formatDate(null), "date unknown");
  assert.equal(formatDate("not a date"), "not a date");
  assert.equal(formatMjd(61273.383), "MJD 61273.383");
  assert.equal(formatMjd(null), "MJD —");
  assert.equal(formatLambda(3.82, 3.9), "3.82–3.90 µm");
  assert.equal(formatScale(6.158), "6.158″/px");
  assert.equal(releaseLabel("qr3"), "QR3");
  assert.equal(visitLabel(frame), "qr3-61273.38316-D5");
  assert.equal(visitLabel({ ...frame, visitId: null }), "—");
});

test("maps plane metadata into frame facts from either visit or epoch rows", () => {
  assert.deepEqual(
    frameFacts({
      visit_id: "dr1-1-D5",
      date: "2026-03-17T07:12:00Z",
      mjd: 61116.3,
      release: "dr1",
    }),
    {
      visitId: "dr1-1-D5",
      epoch: null,
      date: "2026-03-17T07:12:00Z",
      mjd: 61116.3,
      release: "dr1",
    },
  );
  assert.deepEqual(
    frameFacts({ epoch_id: "fixture-dr1", mjd_min: 61116.3, release: "dr1" }),
    {
      visitId: "fixture-dr1",
      epoch: "fixture-dr1",
      date: null,
      mjd: 61116.3,
      release: "dr1",
    },
  );
  assert.equal(frameFacts(null), null);
  assert.equal(frameFacts(undefined), null);
});

test("caption states exactly the four per-frame facts", () => {
  assert.deepEqual(captionFacts(frame), [
    "2026-08-21 09:11Z",
    "MJD 61273.383",
    "QR3",
    "qr3-61273.38316-D5",
  ]);
});

test("readout states exactly the five stack-level facts", () => {
  assert.deepEqual(readoutRows(stack), [
    { label: "Detector", value: "D5" },
    { label: "Matched λ", value: "3.82–3.90 µm" },
    { label: "Pixel scale", value: "6.158″/px" },
    { label: "Units", value: "MJy/sr" },
    { label: "Window hash", value: "36d12ef6e0b4", detail: stack.windowSha },
  ]);
});

test("the canvas alternative names both the frame and the stack facts", () => {
  const alt = canvasAltText(frame, stack);
  for (const expected of [
    "2026-08-21 09:11Z",
    "MJD 61273.383",
    "QR3",
    "qr3-61273.38316-D5",
    "D5",
    "3.82–3.90 µm",
    "6.158″/px",
    "MJy/sr",
  ]) {
    assert.ok(alt.includes(expected), `alt text should name ${expected}`);
  }
});

test("a strip step names its date, MJD, release and visit id", () => {
  assert.equal(
    stepAccessibleName(frame, 0),
    "Step 1: qr3-61273.38316-D5, 2026-08-21 09:11Z, MJD 61273.383, QR3",
  );
  assert.equal(
    stepAccessibleName(
      {
        visitId: "fixture-dr1",
        epoch: "fixture-dr1",
        date: null,
        mjd: 61116.3,
        release: "dr1",
      },
      1,
    ),
    "Step 2: fixture-dr1, date unknown, MJD 61116.300, DR1, epoch fixture-dr1",
  );
  assert.equal(stepAccessibleName(null, 2), "Step 3");
});

test("compare steps show the release while timeline steps show the short date", () => {
  assert.equal(stepShortLabel(frame, false), "2026-08-21");
  assert.equal(stepShortLabel(frame, true), "QR3");
  assert.equal(stepShortLabel(null, true), "—");
});

test("a band-tick row holds one tick per returned compare bin", () => {
  const ticks = bandTicks([
    { band_index: 0, band: 4, lambda_lo_um: 2.92, lambda_hi_um: 3.12 },
    { band_index: 0, band: 4, lambda_lo_um: 2.92, lambda_hi_um: 3.12 },
    { band_index: 1, band: 5, lambda_lo_um: 3.12, lambda_hi_um: 3.31 },
    { band_index: 2, band: 6, lambda_lo_um: 3.31, lambda_hi_um: 3.51 },
  ]);
  assert.deepEqual(ticks, [
    { index: 0, band: 4, lambdaLoUm: 2.92, lambdaHiUm: 3.12 },
    { index: 1, band: 5, lambdaLoUm: 3.12, lambdaHiUm: 3.31 },
    { index: 2, band: 6, lambdaLoUm: 3.31, lambdaHiUm: 3.51 },
  ]);
  assert.deepEqual(bandTicks([]), []);
});

test("scale bar picks a round angular length that fits the gutter", () => {
  const bar = chooseScaleBar(6.158, 512, 660, 300);
  assert.ok(bar);
  assert.equal(bar.label, "20′");
  assert.equal(bar.arcsec, 1200);
  assert.ok(Math.abs(bar.pixels - (1200 / (6.158 * 512 / 660))) < 1e-9);
});

test("scale bar is omitted when the gutter is too narrow", () => {
  assert.equal(chooseScaleBar(6.158, 512, 660, 30), null);
  assert.equal(chooseScaleBar(6.158, 512, 0, 300), null);
  assert.equal(chooseScaleBar(0, 512, 660, 300), null);
  assert.equal(chooseScaleBar(6.158, 0, 660, 300), null);
});

test("cadence positions compress the epoch gap without hiding cadence", () => {
  const positions = cadencePositions([
    { release: "dr1", mjd: 61116.3 },
    { release: "dr1", mjd: 61120.5 },
    { release: "dr1", mjd: 61134.6 },
    { release: "qr3", mjd: 61270.9 },
    { release: "qr3", mjd: 61273.4 },
    { release: "qr3", mjd: 61280.2 },
  ]);
  assert.equal(positions.length, 6);
  assert.equal(positions[0], 0);
  assert.equal(positions[positions.length - 1], 100);
  for (let index = 1; index < positions.length; index += 1) {
    assert.ok(positions[index] > positions[index - 1]);
  }
  const gap = positions[3] - positions[2];
  const within = positions[5] - positions[4];
  assert.ok(gap > within, "the epoch break stays the widest single step");
  assert.ok(gap < 60, "the epoch break is capped so ticks stay readable");
});

test("cadence falls back to even spacing when MJDs are missing", () => {
  assert.deepEqual(cadencePositions([]), []);
  assert.deepEqual(cadencePositions([{ release: "dr1", mjd: null }]), [50]);
  assert.deepEqual(
    cadencePositions([
      { release: "dr1", mjd: 1 },
      { release: "qr3", mjd: null },
      { release: "qr3", mjd: 3 },
    ]),
    [0, 50, 100],
  );
});

test("epoch groups describe each release break between groups", () => {
  const steps = [
    { release: "dr1", mjd: 61116.3 },
    { release: "dr1", mjd: 61120.5 },
    { release: "qr3", mjd: 61270.9 },
    { release: "qr3", mjd: 61273.4 },
  ];
  const groups = epochGroups(steps, [0, 10, 55, 65]);
  assert.equal(groups.length, 2);
  assert.deepEqual(
    groups.map((group) => ({
      release: group.release,
      start: group.start,
      end: group.end,
      gapDays: group.gapDays,
    })),
    [
      { release: "DR1", start: 0, end: 1, gapDays: null },
      { release: "QR3", start: 2, end: 3, gapDays: 150 },
    ],
  );
  assert.equal(groups[1].gapFromPct, 10);
  assert.equal(groups[1].gapToPct, 55);
});
