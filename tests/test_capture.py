"""Offline tests for ferret.capture.

These never touch the live scanner or the daemon. They exercise:
  (a) import smoke  — the module + FerretCamera class import cleanly;
  (b) Y14 unpacker round-trip — pack known 14-bit values, unpack, assert equality;
  (c) Y16 decode of the REAL captured raw bytes reproduces rpc_depth.npy.

The live-capture path (open/get_depth_mm against the device) is NOT tested here —
it requires the physical scanner and the OEM daemon. See test_live_capture_*.
"""
from pathlib import Path

import numpy as np
import pytest

from ferret import capture
from ferret.capture import (
    DEPTH_VALUE_SCALE,
    FerretCamera,
    decode_y16_depth_mm,
    disparity_to_depth_mm,
    pack_y14,
    unpack_y14,
)

PROBE = Path(__file__).resolve().parent.parent / "probe"
RAW_FRAME = PROBE / "rpc_raw_frame.bin"
DEPTH_NPY = PROBE / "rpc_depth.npy"

# The captured raw frame (*.bin) and decoded depth (*.npy) are git-ignored and
# absent on a fresh checkout / in CI. The Y16-decode-of-real-data test is
# skipped (not failed) when either fixture is missing.
requires_real_frame = pytest.mark.skipif(
    not (RAW_FRAME.is_file() and DEPTH_NPY.is_file()),
    reason=f"real capture fixtures missing (git-ignored): {RAW_FRAME}, {DEPTH_NPY}",
)


# (a) ---------------------------------------------------------------- import smoke
def test_import_smoke():
    """Module imports and the public surface exists with the right kinds."""
    assert hasattr(capture, "FerretCamera")
    assert callable(unpack_y14)
    assert callable(pack_y14)
    assert callable(decode_y16_depth_mm)
    assert callable(disparity_to_depth_mm)
    # Class is constructible without opening anything (no socket on __init__).
    cam = FerretCamera()
    assert cam.fmt == capture.OB_FORMAT_Y14
    assert cam._client is None
    assert cam._streaming is False


# (b) -------------------------------------------------------- Y14 round-trip tests
def test_y14_roundtrip_known_values():
    """Pack 4 known 14-bit values into 7 bytes, unpack, assert equality."""
    values = np.array([0, 1, 0x3FFF, 12345], dtype=np.uint16)  # one 4-px group
    packed = pack_y14(values)
    assert len(packed) == 7  # 4 px * 14 bits = 56 bits = 7 bytes
    out = unpack_y14(packed, width=4, height=1)
    assert out.shape == (1, 4)
    np.testing.assert_array_equal(out.ravel(), values)


def test_y14_roundtrip_full_random_frame():
    """A full 1280x800 random 14-bit frame survives pack -> unpack unchanged."""
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 1 << 14, size=(800, 1280), dtype=np.uint16)
    packed = pack_y14(frame)
    assert len(packed) == 1280 * 800 * 14 // 8 == 1_792_000
    out = unpack_y14(packed, width=1280, height=800)
    np.testing.assert_array_equal(out, frame)


def test_y14_packs_bits_little_endian():
    """The 4 pixels occupy bits [0:14],[14:28],[28:42],[42:56] of the 7-byte word."""
    # pixel0 = 1 -> bit 0 set -> first byte == 0x01, rest zero.
    packed = pack_y14(np.array([1, 0, 0, 0], dtype=np.uint16))
    assert packed == b"\x01\x00\x00\x00\x00\x00\x00"
    # pixel1 = 1 -> bit 14 set -> 0x4000 in the low word -> byte index 1 == 0x40.
    packed = pack_y14(np.array([0, 1, 0, 0], dtype=np.uint16))
    assert packed[1] == 0x40
    np.testing.assert_array_equal(unpack_y14(packed, 4, 1).ravel(),
                                  np.array([0, 1, 0, 0]))


def test_y14_rejects_out_of_range():
    with pytest.raises(ValueError):
        pack_y14(np.array([0, 0, 0, 1 << 14], dtype=np.uint32))


def test_y14_rejects_bad_length():
    with pytest.raises(ValueError):
        unpack_y14(b"\x00" * 6, width=4, height=1)  # 4 px needs 7 bytes, not 6


# (c) ----------------------------------------- Y16 decode of the REAL captured data
@requires_real_frame
def test_y16_decode_matches_saved_depth():
    """decode_y16_depth_mm(rpc_raw_frame.bin) reproduces rpc_depth.npy * 0.2."""
    raw_bytes = RAW_FRAME.read_bytes()
    saved = np.load(DEPTH_NPY)  # uint16 800x640, the ground-truth raw frame

    # The raw .bin must be the same 1,024,000-byte blob that was saved as u16.
    assert len(raw_bytes) == saved.size * 2 == 1_024_000

    decoded = decode_y16_depth_mm(raw_bytes)
    assert decoded.shape == saved.shape == (800, 640)
    assert decoded.dtype == np.float32

    # Expected metric depth: raw * 0.2, with 0 and 0xFFFF zeroed out.
    expected = saved.astype(np.float32) * DEPTH_VALUE_SCALE
    invalid = (saved == 0) | (saved == 0xFFFF)
    expected[invalid] = 0.0
    np.testing.assert_array_equal(decoded, expected)

    # And the valid pixels carry real geometry (not all zero).
    assert np.count_nonzero(decoded) > 0


def test_y16_invalid_pixels_zeroed():
    """Sentinel raw values (0, 0xFFFF) decode to 0.0; a real value scales by 0.2."""
    # Use a table-sized frame (512,000 px -> 800x640) so the shape is exact.
    full = np.zeros(512_000, dtype="<u2")
    full[0] = 0          # invalid -> 0.0 mm
    full[1] = 1000       # 1000 * 0.2 -> 200.0 mm
    full[2] = 0xFFFF     # invalid -> 0.0 mm
    flat = decode_y16_depth_mm(full.tobytes()).ravel()
    assert flat[0] == 0.0
    assert flat[1] == pytest.approx(200.0)
    assert flat[2] == 0.0


# Bonus: the disparity->depth path is self-consistent with the logged z0.
def test_disparity_to_depth_consistency_with_z0():
    """At disparity == disp_offset the depth equals z0 (per the calibration)."""
    calib = capture.DEFAULT_CALIB
    # raw such that raw*unit - disp_offset == baseline*focal/z0
    target_disp = calib.baseline * calib.focal_pix / calib.z0
    raw = (target_disp + calib.disp_offset) / calib.unit
    depth = disparity_to_depth_mm(np.array([[raw]], dtype=np.float32), calib)
    assert depth[0, 0] == pytest.approx(calib.z0, rel=1e-4)
    # Zero/negative disparity term -> invalid (0.0).
    low = disparity_to_depth_mm(np.array([[0.0]], dtype=np.float32), calib)
    assert low[0, 0] == 0.0


def test_disparity_rejects_y14_high_sentinel():
    """Regression: raw 0x3FFF (14-bit all-ones, 'no return') -> 0.0, not a bogus
    ~18 mm near-point. The old code only rejected raw 0, leaking the high sentinel."""
    out = disparity_to_depth_mm(np.array([[0x3FFF]], dtype=np.float32))
    assert out[0, 0] == 0.0


def test_ir_switches_stream_and_stops_matching_sensor(monkeypatch):
    class Client:
        calls = []
        sensor = None

        def __init__(self, **kwargs):
            pass

        def call(self, method, *args, **kwargs):
            self.calls.append((method, args))
            if method.startswith("ob_rpc_start_stream"):
                self.sensor = args[0]
            if method.startswith("ob_rpc_get_frame"):
                if self.sensor == capture.OB_SENSOR_DEPTH:
                    return [np.full((800, 640), 1000, dtype="<u2").tobytes()]
                return [bytes([17]) * (800 * 1280)]

        def close(self):
            pass

    monkeypatch.setattr(capture, "MsgpackRpcClient", Client)
    monkeypatch.setattr(FerretCamera, "_discover_suffix", lambda self: "test")
    with FerretCamera() as cam:
        ir = cam.get_ir()
        assert ir.shape == (800, 1280) and np.all(ir == 17)
    stops = [args for method, args in Client.calls if "stop_stream" in method]
    assert [args[:2] for args in stops] == [(capture.OB_SENSOR_DEPTH, capture.OB_FORMAT_Y14),
                                          (capture.OB_SENSOR_IR_RIGHT, capture.OB_FORMAT_Y8)]


def test_depth_rejects_equal_sized_ir_payload():
    cam = FerretCamera()
    with pytest.raises(ValueError, match="Y14"):
        cam._decode_depth(bytes([17]) * (800 * 1280))


def test_y16_rejects_unknown_dimensions():
    with pytest.raises(ValueError, match="Y16"):
        decode_y16_depth_mm(bytes(640 * 2))


def test_explicit_depth_formats_and_sensor_validation():
    y16 = np.full((800, 640), 1000, dtype="<u2").tobytes()
    cam = FerretCamera(depth_format="Y16")
    np.testing.assert_array_equal(cam._decode_depth(y16), np.full((800, 640), 200.0))
    cam._sensor = capture.OB_SENSOR_IR_RIGHT
    with pytest.raises(ValueError, match="depth sensor"):
        cam._decode_depth(y16)
    with pytest.raises(ValueError, match="pixel format"):
        capture.decode_depth_frame(y16, capture.OB_SENSOR_DEPTH, "Y8")
    raw = np.full((800, 1280), 872, dtype=np.uint16)
    np.testing.assert_array_equal(FerretCamera()._decode_depth(pack_y14(raw)),
                                  disparity_to_depth_mm(raw))


def test_depth_switches_back_from_ir_and_rejects_ambiguous_frames():
    calls = []

    class Client:
        def call(self, method, *args, **kwargs):
            calls.append((method, args))
            if "get_frame" in method:
                return [bytes(800 * 1280), bytes(800 * 1280)]

    cam = FerretCamera(depth_format="Y16")
    cam._client, cam._suffix = Client(), "test"
    cam._streaming = True
    cam._sensor, cam._stream_fmt = capture.OB_SENSOR_IR_RIGHT, capture.OB_FORMAT_Y8
    with pytest.raises(ValueError, match="ambiguous"):
        cam.get_depth_mm()
    assert calls[0][1][:2] == (capture.OB_SENSOR_IR_RIGHT, capture.OB_FORMAT_Y8)
    assert calls[1][1][:2] == (capture.OB_SENSOR_DEPTH, capture.OB_FORMAT_Y14)
