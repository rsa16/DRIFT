"""``SPX1`` binary plane format

Layout: 32-byte little-endian header + row-major ``float16`` data.
Don't modify the header layout without creating a new format version and
updating the renderer's reader.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

import numpy as np

MAGIC = b"SPX1"
VERSION = 1
DTYPE_FLOAT16 = 1
HEADER_SIZE = 32
HEADER_FORMAT = "<4sBBHIHHffIHH"
MAX_DIMENSION = 0xFFFF


class PlaneCodecError(ValueError):
    """Raised when a plane blob cannot be encoded or decoded."""


@dataclass(frozen=True)
class PlaneHeader:
    """The ``SPX1`` header."""

    magic: bytes = MAGIC
    version: int = VERSION
    dtype: int = DTYPE_FLOAT16
    channels: int = 0
    flags: int = 0
    height: int = 0
    width: int = 0
    scale: float = 1.0
    offset: float = 0.0
    crc32: int = 0
    frame_index: int = 0
    reserved: int = 0

    def pack(self) -> bytes:
        """Serialise the header to exactly 32 bytes."""
        return struct.pack(
            HEADER_FORMAT,
            self.magic,
            self.version,
            self.dtype,
            self.channels,
            self.flags,
            self.height,
            self.width,
            self.scale,
            self.offset,
            self.crc32,
            self.frame_index,
            self.reserved,
        )

    @classmethod
    def unpack(cls, data: bytes) -> PlaneHeader:
        """Parse a header from the first 32 bytes of ``data``."""
        if len(data) < HEADER_SIZE:
            raise PlaneCodecError(f"SPX1 header requires 32 bytes, got {len(data)}")
        return cls(*struct.unpack_from(HEADER_FORMAT, data, 0))


@dataclass(frozen=True)
class DecodedPlane:
    """A decoded header and its float16 pixel array."""

    header: PlaneHeader
    pixels: np.ndarray


def _validate_header(header: PlaneHeader) -> None:
    if header.magic != MAGIC:
        raise PlaneCodecError(f"bad magic {header.magic!r}; expected {MAGIC!r}")
    if header.version != VERSION:
        raise PlaneCodecError(
            f"unsupported SPX1 version {header.version}; this reader supports "
            f"version {VERSION}"
        )
    if header.dtype != DTYPE_FLOAT16:
        raise PlaneCodecError(
            f"unsupported SPX1 dtype {header.dtype}; only {DTYPE_FLOAT16} "
            "(float16) is allowed"
        )
    if header.height == 0 or header.width == 0:
        raise PlaneCodecError(
            f"SPX1 plane has invalid dimensions {header.height}x{header.width}"
        )


def encode_plane(
    pixels,
    *,
    scale: float = 1.0,
    offset: float = 0.0,
    channels: int = 0,
    flags: int = 0,
    frame_index: int = 0,
    reserved: int = 0,
) -> bytes:
    """Encode a 2-D array as an ``SPX1`` plane."""
    array = np.asarray(pixels)
    if array.ndim != 2:
        raise PlaneCodecError(f"SPX1 plane must be 2-D, got shape {array.shape}")
    height, width = array.shape
    if height > MAX_DIMENSION or width > MAX_DIMENSION:
        raise PlaneCodecError(f"SPX1 dimensions must fit uint16, got {height}x{width}")

    body_array = np.ascontiguousarray(array, dtype=np.float16)
    body = body_array.astype("<f2", copy=False).tobytes(order="C")
    header = PlaneHeader(
        channels=channels,
        flags=flags,
        height=height,
        width=width,
        scale=float(scale),
        offset=float(offset),
        crc32=zlib.crc32(body) & 0xFFFFFFFF,
        frame_index=frame_index,
        reserved=reserved,
    )
    return header.pack() + body


def decode_plane(data: bytes) -> DecodedPlane:
    """Decode and validate an ``SPX1`` plane."""
    header = PlaneHeader.unpack(data)
    _validate_header(header)

    body = data[HEADER_SIZE:]
    expected = header.height * header.width * 2
    if len(body) != expected:
        raise PlaneCodecError(
            f"SPX1 body is {len(body)} bytes; expected {expected} for "
            f"{header.height}x{header.width} float16"
        )
    actual_crc = zlib.crc32(body) & 0xFFFFFFFF
    if actual_crc != header.crc32:
        raise PlaneCodecError(
            f"SPX1 CRC32 mismatch: header {header.crc32:#010x}, body {actual_crc:#010x}"
        )

    pixels = np.frombuffer(body, dtype="<f2").reshape(header.height, header.width)
    return DecodedPlane(header=header, pixels=pixels.copy())
