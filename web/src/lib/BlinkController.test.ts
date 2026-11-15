import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_FPS,
  MAX_FPS,
  MIN_FPS,
  BlinkController,
} from "./BlinkController.ts";

test("blink defaults to 6 fps and clamps to the 1-12 slider range", () => {
  const blink = new BlinkController(8);

  assert.equal(DEFAULT_FPS, 6);
  assert.equal(MIN_FPS, 1);
  assert.equal(MAX_FPS, 12);
  assert.equal(blink.fps, 6);
  assert.equal(blink.frameCount, 8);

  blink.setFps(0);
  assert.equal(blink.fps, 1);
  blink.setFps(99);
  assert.equal(blink.fps, 12);
  blink.setFps(7.6);
  assert.equal(blink.fps, 8);
});

test("spacebar semantics toggle playing without losing the frame", () => {
  const blink = new BlinkController(8);
  blink.goTo(3);

  assert.equal(blink.playing, true);
  blink.toggle();
  assert.equal(blink.playing, false);
  assert.equal(blink.frame, 3);
  blink.toggle();
  assert.equal(blink.playing, true);
});

test("advancing wraps inside the band count", () => {
  const blink = new BlinkController(3);
  assert.equal(blink.advance(1), 1);
  assert.equal(blink.advance(1), 2);
  assert.equal(blink.advance(1), 0);
});
