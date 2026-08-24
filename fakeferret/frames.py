"""Frame payload builders for fakeferret — bit-faithful daemon wire formats.

Every builder returns exactly the bytes the real RPCServer puts in an
``ob_rpc_get_frame`` byte slot (PROTOCOL.md "Frame formats observed"):

* computed metric depth  Y16: uint16 little-endian, mm = value * 0.2,
  0 / 0xFFFF invalid. 640x800 -> 1,024,000 bytes.
* raw disparity          Y14: 14-bit packed, 4 px per 7 bytes laid out
  little-endian (pixel i occupies bits [14i : 14i+14] of the 56-bit word),
  1280x800 -> 1,792,000 bytes. Sentinel 0x3FFF = no return.
* IR                     Y8 : uint8, 1280x800 -> 1,024,000 bytes.

The Y14 packer here is written FROM THE PROTOCOL SPEC (not imported from
ferret.capture) so simulator tests are not circular: fakeferret packs from the
documented bit layout, FerretCamera unpacks with its own implementation.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

# --- geometry ------------------------------------------------------------------
NATIVE_W, NATIVE_H = 1280, 800   # raw sensor / Y14 / Y8 resolution
DEPTH_W, DEPTH_H = 640, 800      # computed-depth (Y16) resolution

Y16_FULL_BYTES = DEPTH_W * DEPTH_H * 2          # 1,024,000
Y14_FULL_BYTES = NATIVE_W * NATIVE_H * 14 // 8  # 1,792,000
IR_FULL_BYTES = NATIVE_W * NATIVE_H             # 1,024,000

DEPTH_VALUE_SCALE = 0.2     # Y16 raw -> millimetres (PROTOCOL.md)
Y14_SENTINEL = 0x3FFF       # 14-bit all-ones = no return

# FerretDevice.cpp calibration (PROTOCOL.md) — needed to synthesise a
# self-consistent disparity frame from a depth image.
BASELINE_MM = 149.79
FOCAL_PIX = 956.671         # native 1280-wide sensor
DISP_OFFSET = 218.0
UNIT = 0.5

GOLDEN_NPY = Path(__file__).resolve().parent.parent / "probe" / "rpc_depth.npy"


def depth_u16_payload(depth_mm: np.ndarray) -> bytes:
    """Encode float millimetre depths as a wire-faithful Y16 payload."""
    mm = np.asarray(depth_mm, dtype=np.float64)
    if mm.shape != (DEPTH_H, DEPTH_W):
        raise ValueError(f"expected {(DEPTH_H, DEPTH_W)} depth, got {mm.shape}")
    raw = np.rint(mm / DEPTH_VALUE_SCALE)
    raw = np.clip(raw, 0, 0xFFFE).astype("<u2")  # never emit accidental 0xFFFF
    return raw.tobytes()


def pack_y14(values: np.ndarray) -> bytes:
    """Pack 14-bit disparities into the daemon's 4-px-per-7-bytes layout.

    Spec (PROTOCOL.md + ferret.capture.unpack_y14 docstring): each group of 4
    pixels forms a 56-bit little-endian word; pixel i sits in bits [14i:14i+14].
    """
    flat = np.asarray(values, dtype=np.uint64).ravel()
    if flat.size % 4 != 0:
        raise ValueError("value count must be a multiple of 4")
    if flat.size and int(flat.max()) > Y14_SENTINEL:
        raise ValueError("values exceed 14-bit range")
    groups = flat.reshape(-1, 4)
    word = np.zeros(groups.shape[0], dtype=np.uint64)
    for px in range(4):
        word |= groups[:, px] << np.uint64(14 * px)
    out = np.empty((groups.shape[0], 7), dtype=np.uint8)
    for byte_index in range(7):
        out[:, byte_index] = (
            (word >> np.uint64(8 * byte_index)) & np.uint64(0xFF)
        ).astype(np.uint8)
    return out.tobytes()


def synthetic_depth_u16(value=1000, invalid_corners=True) -> bytes:
    """A complete 800x640 Y16 frame with known values (default raw 1000 ->
    200.0 mm), plus two sentinel corners so invalid handling is exercised."""
    raw = np.full((DEPTH_H, DEPTH_W), int(value), dtype="<u2")
    if invalid_corners:
        raw[0, 0] = 0
        raw[-1, -1] = 0xFFFF
    return raw.tobytes()


def synthetic_disparity(depth_mm: np.ndarray | None = None,
                        constant_raw: int | None = None) -> bytes:
    """A complete 1280x800 raw-Y14 disparity frame.

    Either invert a depth image through the device calibration
    (raw = (baseline*focal/depth_mm + disp_offset) / unit) or fill a constant
    raw value. Invalid pixels become the 0x3FFF no-return sentinel.
    """
    n_px = NATIVE_W * NATIVE_H
    if constant_raw is not None:
        raw = np.full(n_px, int(constant_raw), dtype=np.uint64)
    else:
        if depth_mm is None:
            raise ValueError("need depth_mm or constant_raw")
        mm = np.asarray(depth_mm, dtype=np.float64).ravel()
        if mm.size != n_px:
            raise ValueError(f"need {n_px} pixels, got {mm.size}")
        raw = np.full(n_px, Y14_SENTINEL, dtype=np.uint64)
        valid = mm > 0
        raw[valid] = np.rint(
            (BASELINE_MM * FOCAL_PIX / mm[valid] + DISP_OFFSET) / UNIT
        )
        raw[~valid] = Y14_SENTINEL
        # Anything rounding up to the sentinel is treated as no-return.
        raw = np.where(raw >= Y14_SENTINEL, Y14_SENTINEL, raw)
    return pack_y14(raw)


def flooded_sentinel_y14() -> bytes:
    """Full-size Y14 frame where EVERY pixel is the 0x3FFF sentinel.

    Four 14-bit all-ones pixels make the whole 56-bit group all ones, so the
    packed frame is simply 0xFF repeated 1,792,000 times.
    """
    return b"\xff" * Y14_FULL_BYTES


def ir_payload(value: int = 128) -> bytes:
    """A complete 1280x800 Y8 IR frame filled with ``value``."""
    return np.full((NATIVE_H, NATIVE_W), int(value), dtype=np.uint8).tobytes()


def golden_y16_payload(path: Path = GOLDEN_NPY) -> bytes:
    """The committed probe/rpc_depth.npy frame as raw wire bytes.

    The probe saved the u16 array straight off the wire
    (probe/ferret_rpc_depth.py:120-133), so ``astype('<u2').tobytes()`` is the
    original payload, byte for byte.
    """
    arr = np.load(path)
    if arr.dtype != np.uint16 or arr.shape != (DEPTH_H, DEPTH_W):
        raise ValueError(f"{path} is not a uint16 ({DEPTH_H},{DEPTH_W}) frame")
    return arr.astype("<u2", copy=False).tobytes()
