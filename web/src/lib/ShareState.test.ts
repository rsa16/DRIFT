import assert from "node:assert/strict";
import test from "node:test";

import { formatShareQuery, parseShareState } from "./ShareState.ts";

test("parses ra, dec and fov from a shareable query", () => {
  assert.deepEqual(parseShareState("?ra=269.452075&dec=4.693365&fov=0.87"), {
    ra: 269.452075,
    dec: 4.693365,
    fov: 0.87,
  });
});

test("requires all three values and rejects out-of-range coordinates", () => {
  assert.equal(parseShareState(""), null);
  assert.equal(parseShareState("?ra=10&dec=2"), null);
  assert.equal(parseShareState("?ra=400&dec=2&fov=0.5"), null);
  assert.equal(parseShareState("?ra=10&dec=-91&fov=0.5"), null);
  assert.equal(parseShareState("?ra=10&dec=2&fov=0"), null);
  assert.equal(parseShareState("?ra=nope&dec=2&fov=0.5"), null);
});

test("round-trips a view into the URL query", () => {
  const state = { ra: 269.452075, dec: 4.693365, fov: 0.87 };
  const query = formatShareQuery(state);

  assert.match(query, /^ra=269\.452075&dec=4\.693365&fov=0\.87$/);
  assert.deepEqual(parseShareState(`?${query}`), state);
});
