import struct

import numpy as np
import pytest

from drift.planes import PlaneCodecError, decode_plane, encode_plane


def test_round_trip_preserves_float16_values():
    values = np.array(
        [
            [0.0, -0.0, 1.5, np.nan, 65504.0],
            [-2.25, np.inf, -np.inf, 0.0001, -0.0],
        ],
        dtype=np.float16,
    )

    blob = encode_plane(
        values,
        scale=0.25,
        offset=-1.5,
        channels=3,
        flags=5,
        frame_index=7,
    )
    decoded = decode_plane(blob)

    assert decoded.header.magic == b"SPX1"
    assert decoded.header.version == 1
    assert decoded.header.dtype == 1
    assert decoded.header.channels == 3
    assert decoded.header.flags == 5
    assert decoded.header.height == 2
    assert decoded.header.width == 5
    assert decoded.header.scale == pytest.approx(0.25)
    assert decoded.header.offset == pytest.approx(-1.5)
    assert decoded.header.frame_index == 7
    assert decoded.header.reserved == 0
    assert decoded.pixels.dtype == np.float16
    assert np.array_equal(decoded.pixels.view(np.uint16), values.view(np.uint16))


def test_header_offsets_match_the_spec():
    blob = encode_plane(
        np.zeros((3, 2), dtype=np.float16),
        channels=0x0102,
        flags=0x03040506,
        scale=1.25,
        offset=-0.5,
        frame_index=0x0708,
    )

    assert len(blob) == 32 + 3 * 2 * 2
    assert struct.unpack_from("<4s", blob, 0)[0] == b"SPX1"
    assert struct.unpack_from("<B", blob, 4)[0] == 1  # version
    assert struct.unpack_from("<B", blob, 5)[0] == 1  # dtype float16
    assert struct.unpack_from("<H", blob, 6)[0] == 0x0102  # channels
    assert struct.unpack_from("<I", blob, 8)[0] == 0x03040506  # flags
    assert struct.unpack_from("<H", blob, 12)[0] == 3  # height
    assert struct.unpack_from("<H", blob, 14)[0] == 2  # width
    assert struct.unpack_from("<f", blob, 16)[0] == pytest.approx(1.25)
    assert struct.unpack_from("<f", blob, 20)[0] == pytest.approx(-0.5)
    assert struct.unpack_from("<I", blob, 24)[0] == decode_plane(blob).header.crc32
    assert struct.unpack_from("<H", blob, 28)[0] == 0x0708  # frame index
    assert struct.unpack_from("<H", blob, 30)[0] == 0  # reserved


def test_crc_mismatch_is_rejected():
    blob = bytearray(encode_plane(np.arange(9, dtype=np.float16).reshape(3, 3)))
    blob[-1] ^= 0xFF

    with pytest.raises(PlaneCodecError, match="CRC32"):
        decode_plane(bytes(blob))


def test_unknown_version_is_rejected():
    blob = bytearray(encode_plane(np.zeros((1, 1), dtype=np.float16)))
    blob[4] = 2

    with pytest.raises(PlaneCodecError, match="version"):
        decode_plane(bytes(blob))


def test_non_float16_dtype_is_rejected():
    blob = bytearray(encode_plane(np.zeros((1, 1), dtype=np.float16)))
    blob[5] = 2

    with pytest.raises(PlaneCodecError, match="dtype"):
        decode_plane(bytes(blob))


def test_bad_magic_and_truncated_input_are_rejected():
    blob = bytearray(encode_plane(np.zeros((1, 1), dtype=np.float16)))
    blob[0] = ord("X")

    with pytest.raises(PlaneCodecError, match="magic"):
        decode_plane(bytes(blob))

    with pytest.raises(PlaneCodecError, match="32 bytes"):
        decode_plane(b"SPX1")
