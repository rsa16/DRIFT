export const SPX1_HEADER_BYTES = 32;
export const SPX1_VERSION = 1;
export const SPX1_DTYPE_FLOAT16 = 1;

export const TEXTURE_FORMAT = {
  internalFormat: "R16F",
  format: "RED",
  type: "HALF_FLOAT",
} as const;

export interface PlaneHeader {
  magic: string;
  version: number;
  dtype: number;
  channels: number;
  flags: number;
  height: number;
  width: number;
  scale: number;
  offset: number;
  crc32: number;
  frameIndex: number;
  reserved: number;
}

export interface DecodedPlane {
  header: PlaneHeader;
  width: number;
  height: number;
  data: Uint16Array;
}

export class Spx1Error extends Error {
  constructor(message: string) {
    super(message);
    this.name = "Spx1Error";
  }
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[index] = value >>> 0;
  }
  return table;
})();

/** IMPORTANT - must match Python's crc32 impl */
export function crc32(
  bytes: Uint8Array,
  start = 0,
  end = bytes.length,
): number {
  let crc = 0xffffffff;
  for (let index = start; index < end; index += 1) {
    crc = CRC_TABLE[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function headerMagic(view: DataView): string {
  return String.fromCharCode(
    view.getUint8(0),
    view.getUint8(1),
    view.getUint8(2),
    view.getUint8(3),
  );
}

/**
 * Decode and validate an `SPX1` plane.
 *
 * Throws `Spx1Error` for a bad magic, version, dtype, length or CRC.
 */
export function decodeSpx1(buffer: ArrayBuffer): DecodedPlane {
  if (buffer.byteLength < SPX1_HEADER_BYTES) {
    throw new Spx1Error(
      `SPX1 header requires ${SPX1_HEADER_BYTES} bytes, got ${buffer.byteLength}`,
    );
  }
  const bytes = new Uint8Array(buffer);
  const view = new DataView(buffer);
  const magic = headerMagic(view);
  if (magic !== "SPX1") {
    throw new Spx1Error(`bad SPX1 magic ${JSON.stringify(magic)}`);
  }
  const version = view.getUint8(4);
  if (version !== SPX1_VERSION) {
    throw new Spx1Error(
      `unsupported SPX1 version ${version}; expected ${SPX1_VERSION}`,
    );
  }
  const dtype = view.getUint8(5);
  if (dtype !== SPX1_DTYPE_FLOAT16) {
    throw new Spx1Error(
      `unsupported SPX1 dtype ${dtype}; only float16 (${SPX1_DTYPE_FLOAT16}) is allowed`,
    );
  }
  const height = view.getUint16(12, true);
  const width = view.getUint16(14, true);
  if (height === 0 || width === 0) {
    throw new Spx1Error(`invalid SPX1 dimensions ${height}x${width}`);
  }
  const expected = SPX1_HEADER_BYTES + height * width * 2;
  if (buffer.byteLength !== expected) {
    throw new Spx1Error(
      `SPX1 body is ${buffer.byteLength - SPX1_HEADER_BYTES} bytes; ` +
        `expected ${height * width * 2} for ${height}x${width} float16`,
    );
  }
  const declaredCrc = view.getUint32(24, true);
  const actualCrc = crc32(bytes, SPX1_HEADER_BYTES);
  if (declaredCrc !== actualCrc) {
    throw new Spx1Error(
      `SPX1 CRC32 mismatch: header ${declaredCrc.toString(16)}, ` +
        `body ${actualCrc.toString(16)}`,
    );
  }
  const header: PlaneHeader = {
    magic,
    version,
    dtype,
    channels: view.getUint16(6, true),
    flags: view.getUint32(8, true),
    height,
    width,
    scale: view.getFloat32(16, true),
    offset: view.getFloat32(20, true),
    crc32: declaredCrc,
    frameIndex: view.getUint16(28, true),
    reserved: view.getUint16(30, true),
  };
  const data = new Uint16Array(buffer, SPX1_HEADER_BYTES, height * width);
  return { header, height, width, data };
}
