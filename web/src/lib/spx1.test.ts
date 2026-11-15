import assert from "node:assert/strict";
import test from "node:test";

import {
  SPX1_HEADER_BYTES,
  Spx1Error,
  TEXTURE_FORMAT,
  crc32,
  decodeSpx1,
} from "./spx1.ts";

function buildPlane({
  magic = "SPX1",
  version = 1,
  dtype = 1,
  channels = 3,
  flags = 7,
  height = 2,
  width = 2,
  scale = 1.5,
  offset = -0.5,
  frameIndex = 4,
  pixels = new Uint16Array([0x3c00, 0x4000, 0x4200, 0x4400]),
  corruptCrc = false,
}: {
  magic?: string;
  version?: number;
  dtype?: number;
  channels?: number;
  flags?: number;
  height?: number;
  width?: number;
  scale?: number;
  offset?: number;
  frameIndex?: number;
  pixels?: Uint16Array;
  corruptCrc?: boolean;
} = {}): ArrayBuffer {
  const buffer = new ArrayBuffer(SPX1_HEADER_BYTES + pixels.length * 2);
  const bytes = new Uint8Array(buffer);
  const view = new DataView(buffer);
  bytes.set(new TextEncoder().encode(magic), 0);
  view.setUint8(4, version);
  view.setUint8(5, dtype);
  view.setUint16(6, channels, true);
  view.setUint32(8, flags, true);
  view.setUint16(12, height, true);
  view.setUint16(14, width, true);
  view.setFloat32(16, scale, true);
  view.setFloat32(20, offset, true);
  view.setUint16(28, frameIndex, true);
  view.setUint16(30, 0, true);
  new Uint8Array(buffer, SPX1_HEADER_BYTES).set(
    new Uint8Array(pixels.buffer, pixels.byteOffset, pixels.byteLength),
  );
  const bodyCrc = crc32(bytes, SPX1_HEADER_BYTES);
  view.setUint32(24, corruptCrc ? bodyCrc ^ 0xffffffff : bodyCrc, true);
  return buffer;
}

test("crc32 matches the standard 123456789 vector", () => {
  const bytes = new TextEncoder().encode("123456789");
  assert.equal(crc32(bytes), 0xcbf43926);
});

test("decodes an SPX1 plane into float16 bits and metadata", () => {
  const decoded = decodeSpx1(buildPlane());

  assert.equal(decoded.header.magic, "SPX1");
  assert.equal(decoded.header.version, 1);
  assert.equal(decoded.header.dtype, 1);
  assert.equal(decoded.header.channels, 3);
  assert.equal(decoded.header.flags, 7);
  assert.equal(decoded.header.height, 2);
  assert.equal(decoded.header.width, 2);
  assert.equal(decoded.header.scale, 1.5);
  assert.equal(decoded.header.offset, -0.5);
  assert.equal(decoded.header.frameIndex, 4);
  assert.equal(decoded.width, 2);
  assert.equal(decoded.height, 2);
  assert.deepEqual([...decoded.data], [0x3c00, 0x4000, 0x4200, 0x4400]);
  assert.ok(decoded.data instanceof Uint16Array);
});

test("rejects bad magic rather than drawing it", () => {
  assert.throws(() => decodeSpx1(buildPlane({ magic: "NOPE" })), Spx1Error);
});

test("rejects a body with the wrong CRC rather than drawing it", () => {
  assert.throws(() => decodeSpx1(buildPlane({ corruptCrc: true })), Spx1Error);
});

test("rejects non-float16 planes and unknown versions", () => {
  assert.throws(() => decodeSpx1(buildPlane({ dtype: 2 })), Spx1Error);
  assert.throws(() => decodeSpx1(buildPlane({ version: 2 })), Spx1Error);
});

test("rejects truncated headers and bodies", () => {
  assert.throws(() => decodeSpx1(new ArrayBuffer(8)), Spx1Error);
  const truncated = buildPlane();
  assert.throws(() => decodeSpx1(truncated.slice(0, truncated.byteLength - 2)), Spx1Error);
});

test("the renderer's only texture format is R16F, never 8-bit", () => {
  assert.equal(TEXTURE_FORMAT.internalFormat, "R16F");
  assert.equal(TEXTURE_FORMAT.format, "RED");
  assert.equal(TEXTURE_FORMAT.type, "HALF_FLOAT");
  assert.ok(!JSON.stringify(TEXTURE_FORMAT).includes("UNSIGNED_BYTE"));
});
