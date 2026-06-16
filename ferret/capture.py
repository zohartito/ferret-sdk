"""FerretCamera: pull depth/IR frames from the CR-Scan Ferret over the OEM
daemon's msgpack-RPC channel (no USB claim, no root).

This refactors ``probe/ferret_rpc_depth.py`` + ``probe/rpc_client.py`` into a
clean class. Two decode paths are supported:

* **Y16 computed depth** — the daemon, in scan/depth-processing mode, returns
  metric depth as little-endian uint16 where ``mm = value * DEPTH_VALUE_SCALE``
  (0.2). 0 and 0xFFFF are invalid. Frame size 1,024,000 bytes = 800x640.
* **Y14 raw disparity** — in raw mode the daemon returns 14-bit packed disparity
  (4 px per 7 bytes), full 1280x800 = 1,792,000 bytes. We unpack, convert
  disparity -> depth via the device calibration, and return metric depth.

Per-device RPC methods carry a suffix = the depth-camera USB interface path with
the dot dropped (e.g. ``1-2-1.0`` -> ``1-2-10``). The suffix changes on replug,
so it is auto-discovered from the daemon log and verified against the server.

SAFETY: importing this module touches nothing. Only ``FerretCamera.open()``
opens a socket to 127.0.0.1:10010. The decode/unpack helpers are pure functions
and are what the offline tests exercise.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .rpc_client import MsgpackRpcClient, RpcError

# --- Orbbec sensor / format enums (from PROTOCOL.md) ---------------------------
OB_SENSOR_DEPTH = 3
OB_SENSOR_IR = 1
OB_SENSOR_IR_RIGHT = 7
OB_FORMAT_Y8 = 9
OB_FORMAT_Y14 = 24

# --- Frame geometry -----------------------------------------------------------
NATIVE_WIDTH, NATIVE_HEIGHT = 1280, 800   # IR sensor / raw Y14 resolution
DEPTH_WIDTH, DEPTH_HEIGHT = 640, 800      # computed-depth (Y16) resolution
DEFAULT_FPS = 30

# Frame byte sizes -> (height, width). Used to interpret a Y16 byte blob.
# Y16 is 2 bytes/pixel, so byte_count // 2 must equal width * height. Only the
# 800x640 entry is verified against real captured data; other sizes fall through
# to the 640-wide heuristic in decode_y16_depth_mm rather than risk a guessed
# shape that would raise on reshape.
_Y16_SHAPE_TABLE = {
    1_024_000: (800, 640),  # 800 * 640 * 2 = 1,024,000 — verified on real data
}

DEPTH_VALUE_SCALE = 0.2     # Y16 depth value -> millimetres
Y16_INVALID = (0, 0xFFFF)   # sentinel raw values
Y14_INVALID = 0x3FFF        # 14-bit all-ones = "no return" in raw disparity
MIN_VALID_DEPTH_MM = 50.0   # close-range floor; reject bogus near-points

LOG_PATH = Path("/Library/Creality/Log/ScannerStreamSDK.log")
CONTROL_PORT = 10010


@dataclass(frozen=True)
class FerretCalibration:
    """Disparity -> depth parameters, read from the device log / FerretDevice.cpp.

    depth_mm = baseline * focal_pix / (raw_disparity * unit - disp_offset)
    """
    baseline: float = 149.79      # mm
    focal_pix: float = 956.671    # px (native 1280x800 IR sensor)
    z0: float = 657.34            # mm, reference depth (disp == disp_offset)
    disp_offset: float = 218.0
    unit: float = 0.5             # disparity sub-pixel unit


DEFAULT_CALIB = FerretCalibration()


# ---------------------------------------------------------------------------
# Pure decode helpers (no device, no sockets) — these are what tests cover.
# ---------------------------------------------------------------------------
def unpack_y14(packed: bytes, width: int = NATIVE_WIDTH,
               height: int = NATIVE_HEIGHT) -> np.ndarray:
    """Unpack 14-bit little-endian packed pixels into a uint16 HxW array.

    Packing groups 4 pixels into 7 bytes (4 * 14 = 56 bits). Bits are laid out
    little-endian: the 56-bit little-endian integer of the 7 bytes holds pixel0
    in bits [0:14], pixel1 in [14:28], pixel2 in [28:42], pixel3 in [42:56].
    """
    n_pixels = width * height
    if n_pixels % 4 != 0:
        raise ValueError(f"pixel count {n_pixels} is not a multiple of 4")
    n_groups = n_pixels // 4
    expected = n_groups * 7
    if len(packed) != expected:
        raise ValueError(
            f"Y14 byte length {len(packed)} != expected {expected} "
            f"for {width}x{height}")

    raw = np.frombuffer(packed, dtype=np.uint8).reshape(n_groups, 7).astype(np.uint64)
    # Reconstruct the 56-bit little-endian word per group.
    word = np.zeros(n_groups, dtype=np.uint64)
    for byte_index in range(7):
        word |= raw[:, byte_index] << np.uint64(8 * byte_index)

    mask = np.uint64(0x3FFF)
    out = np.empty((n_groups, 4), dtype=np.uint16)
    for px in range(4):
        out[:, px] = ((word >> np.uint64(14 * px)) & mask).astype(np.uint16)
    return out.reshape(height, width)


def pack_y14(values: np.ndarray) -> bytes:
    """Inverse of :func:`unpack_y14` — pack 14-bit values into 7-byte groups.

    Provided so the unpacker can be round-trip tested without the device.
    Values must be < 2**14 and the total count a multiple of 4.
    """
    flat = np.asarray(values, dtype=np.uint64).ravel()
    if flat.size % 4 != 0:
        raise ValueError(f"value count {flat.size} is not a multiple of 4")
    if flat.size and int(flat.max()) > 0x3FFF:
        raise ValueError("values exceed 14-bit range (>16383)")

    groups = flat.reshape(-1, 4)
    word = np.zeros(groups.shape[0], dtype=np.uint64)
    for px in range(4):
        word |= groups[:, px] << np.uint64(14 * px)

    out = np.empty((groups.shape[0], 7), dtype=np.uint8)
    for byte_index in range(7):
        out[:, byte_index] = ((word >> np.uint64(8 * byte_index)) & np.uint64(0xFF)).astype(np.uint8)
    return out.tobytes()


def disparity_to_depth_mm(raw_disparity: np.ndarray,
                          calib: FerretCalibration = DEFAULT_CALIB) -> np.ndarray:
    """Convert raw Y14 disparity to metric depth in millimetres.

    depth_mm = baseline * focal_pix / (raw * unit - disp_offset)

    For these constants baseline*focal_pix/z0 == 217.9994 ~= disp_offset (218),
    so raw ~= 872 (raw*unit == 2*disp_offset) maps to z0; this is a numerical
    coincidence of the logged constants, not an identity raw*unit == disp_offset.

    Invalid pixels map to 0: the all-ones 14-bit sentinel (0x3FFF, "no return"),
    a non-positive disparity term, or a depth outside the plausible close-range
    band. Returns float32 HxW with the same shape as ``raw_disparity``.
    """
    raw = np.asarray(raw_disparity, dtype=np.float32)
    disparity = raw * calib.unit - calib.disp_offset
    depth = np.zeros_like(raw, dtype=np.float32)
    valid = (disparity > 0) & (raw < Y14_INVALID)
    depth[valid] = (calib.baseline * calib.focal_pix) / disparity[valid]
    # Reject absurdly-near survivors (e.g. raw just under the sentinel).
    depth[depth < MIN_VALID_DEPTH_MM] = 0.0
    return depth


def decode_y16_depth_mm(frame_bytes: bytes) -> np.ndarray:
    """Decode a Y16 computed-depth byte blob to metric depth (float32 HxW).

    Invalid raw values (0 and 0xFFFF) become 0.0 mm.
    """
    u16 = np.frombuffer(frame_bytes, dtype="<u2")
    shape = _Y16_SHAPE_TABLE.get(len(frame_bytes))
    if shape is None:
        # Fall back to a 640-wide frame, matching the probe's heuristic.
        shape = (u16.size // DEPTH_WIDTH, DEPTH_WIDTH)
    depth_raw = u16.reshape(shape)
    mm = depth_raw.astype(np.float32) * DEPTH_VALUE_SCALE
    invalid = (depth_raw == Y16_INVALID[0]) | (depth_raw == Y16_INVALID[1])
    mm[invalid] = 0.0
    return mm


def _candidate_suffixes(log_text: str) -> list[str]:
    """Device RPC suffixes derived from the depth-camera USB path in the log,
    most-recent first. ``1-2-1.0`` -> ``1-2-10`` is the canonical one; a few
    spelling fallbacks are appended for robustness across log formats."""
    candidates: list[str] = []
    for full in reversed(re.findall(
            r"([0-9][0-9.\-]+) \| CR-Scan Ferret Depth Camera", log_text)):
        for cand in (re.sub(r"\.(\d+)$", r"\1", full),  # 1-2-1.0 -> 1-2-10
                     full.replace(".", ""),
                     full.replace(".", "-"),
                     full.rsplit(".", 1)[0]):
            if cand and cand not in candidates:
                candidates.append(cand)
    return candidates


class FerretCamera:
    """RPC client for the Ferret depth camera via the OEM daemon.

    Typical use::

        cam = FerretCamera()
        cam.open()                 # connect + discover suffix + start stream
        depth_mm = cam.get_depth_mm()
        cam.close()

    Or as a context manager::

        with FerretCamera() as cam:
            depth_mm = cam.get_depth_mm()

    NOTE: ``open()`` / ``get_*`` talk to the live daemon. They are NOT exercised
    by the offline test suite. The decode logic (Y16/Y14) is verified separately
    via the module-level pure functions.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = CONTROL_PORT,
                 fmt: int = OB_FORMAT_Y14, timeout: float = 8.0,
                 calib: FerretCalibration = DEFAULT_CALIB,
                 log_path: Path = LOG_PATH):
        self.host = host
        self.port = port
        self.fmt = fmt
        self.timeout = timeout
        self.calib = calib
        self.log_path = Path(log_path)
        self._client: Optional[MsgpackRpcClient] = None
        self._suffix: Optional[str] = None
        self._streaming = False

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> "FerretCamera":
        """Connect to the daemon, discover the device suffix, start the stream."""
        self._client = MsgpackRpcClient(host=self.host, port=self.port,
                                        timeout=self.timeout)
        self._suffix = self._discover_suffix()
        if self._suffix is None:
            self.close()
            raise RpcError(
                "could not discover device RPC suffix — is the daemon up and the "
                "scanner connected? (open CrealityScan once)")
        self._start_stream(OB_SENSOR_DEPTH, self.fmt)
        return self

    def close(self) -> None:
        """Stop the stream (best-effort) and close the socket."""
        if self._client is not None:
            if self._streaming and self._suffix is not None:
                try:  # GUI uses the 5-arg form; matches start_stream for a clean stop
                    self._client.call(self._method("ob_rpc_stop_stream"),
                                      OB_SENSOR_DEPTH, self.fmt, NATIVE_WIDTH,
                                      NATIVE_HEIGHT, DEFAULT_FPS, timeout=3.0)
                except Exception:
                    pass  # closing the socket also stops the stream
            self._client.close()
        self._client = None
        self._streaming = False

    def __enter__(self) -> "FerretCamera":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- public frame API ---------------------------------------------------
    def get_depth_mm(self, max_polls: int = 150) -> np.ndarray:
        """Return one metric-depth frame as float32 HxW (0.0 = invalid).

        Decodes Y16 computed depth or Y14 raw disparity depending on the frame
        size the daemon delivers (the daemon's processing mode decides which).
        """
        blob = self._grab_frame(max_polls=max_polls)
        return self._decode_depth(blob)

    def get_ir(self, max_polls: int = 150) -> np.ndarray:
        """Return one IR frame as a uint8 HxW array (Y8)."""
        blob = self._grab_frame(max_polls=max_polls)
        arr = np.frombuffer(blob, dtype=np.uint8)
        if arr.size == NATIVE_WIDTH * NATIVE_HEIGHT:
            return arr.reshape(NATIVE_HEIGHT, NATIVE_WIDTH)
        return arr.reshape(arr.size // NATIVE_WIDTH, NATIVE_WIDTH)

    # -- decode dispatch ----------------------------------------------------
    def _decode_depth(self, blob: bytes) -> np.ndarray:
        """Pick the Y16 or Y14 decode path from the frame byte length."""
        size = len(blob)
        y14_full = NATIVE_WIDTH * NATIVE_HEIGHT * 14 // 8  # 1,792,000
        if size == y14_full:
            raw_disp = unpack_y14(blob, NATIVE_WIDTH, NATIVE_HEIGHT)
            return disparity_to_depth_mm(raw_disp, self.calib)
        if size % 2 == 0:
            return decode_y16_depth_mm(blob)
        raise ValueError(f"unrecognised depth frame size: {size} bytes")

    # -- low-level RPC ------------------------------------------------------
    def _method(self, base: str) -> str:
        return f"{base}{self._suffix}"

    def _discover_suffix(self) -> Optional[str]:
        try:
            log_text = self.log_path.read_text(errors="ignore")
        except OSError:
            return None
        for suffix in _candidate_suffixes(log_text):
            self._suffix = suffix
            try:
                self._client.call(self._method("ob_get_stream_profile_list"),
                                  timeout=3.0)
                return suffix
            except RpcError as err:
                if "could not find function" in str(err):
                    continue
                return suffix  # device matched; errored for an unrelated reason
            except Exception:
                continue
        return None

    def _start_stream(self, sensor: int, fmt: int) -> None:
        self._client.call(self._method("ob_rpc_start_stream"),
                          sensor, fmt, NATIVE_WIDTH, NATIVE_HEIGHT, DEFAULT_FPS,
                          timeout=self.timeout)
        self._streaming = True

    def _grab_frame(self, max_polls: int = 150) -> bytes:
        """Poll get_frame until a large byte slot (the image) shows up."""
        if self._client is None or self._suffix is None:
            raise RuntimeError("camera not open — call open() first")
        for _ in range(max_polls):
            frameset = self._client.call(self._method("ob_rpc_get_frame"),
                                         timeout=self.timeout)
            slots = [bytes(x) for x in frameset
                     if isinstance(x, (bytes, bytearray)) and len(x) > 1000]
            if slots:
                return max(slots, key=len)
            time.sleep(0.04)  # give the sensor time to warm up / produce a frame
        raise TimeoutError("no non-empty frame received within poll budget")
