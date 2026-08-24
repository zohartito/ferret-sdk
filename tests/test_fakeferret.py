"""Integration tests: FerretCamera (UNMODIFIED, host/port injected only)
against the fakeferret daemon simulator on 127.0.0.1:20010+.

Covers:
  * golden replay — the committed probe/rpc_depth.npy served as a Y16 payload
    reproduces BYTE-EXACT through FerretCamera's public API;
  * both daemon modes — computed Y16 depth AND raw Y14 disparity (+ Y8 IR);
  * failure injection — truncated payload, 0x3FFF-flooded frame, mid-stream
    disconnect: clear errors / all-invalid frames, never garbage geometry;
  * suffix discovery against the simulator (right and wrong suffix).

The real daemon on ports 10010-10013 is never contacted.
"""
from __future__ import annotations

import socket
from pathlib import Path

import numpy as np
import pytest

from fakeferret import (
    Y14_FULL_BYTES,
    Y16_FULL_BYTES,
    FakeFerretDaemon,
    flooded_sentinel_y14,
    golden_y16_payload,
    ir_payload,
    synthetic_depth_u16,
    synthetic_disparity,
)
from ferret import capture
from ferret.rpc_client import MsgpackRpcClient, RpcError

PROJECT = Path(__file__).resolve().parents[1]
GOLDEN_NPY = PROJECT / "probe" / "rpc_depth.npy"

# fakeferret only ever listens on 20010+; pick a free one deterministically.
_PORT_COUNTER = [20120]


def _pick_port() -> int:
    port = _PORT_COUNTER[0]
    _PORT_COUNTER[0] += 1
    return port


def _daemon_log(tmp_path, usb_path="2-2-1.0"):
    log = tmp_path / "ScannerStreamSDK.log"
    log.write_text(f"{usb_path} | CR-Scan Ferret Depth Camera\n")
    return log


def _open_camera(daemon, tmp_path, **kwargs):
    cam = capture.FerretCamera(
        host="127.0.0.1", port=daemon.bound_port,
        log_path=_daemon_log(tmp_path), **kwargs,
    )
    return cam.open()


def _start_daemon(**kwargs):
    kwargs.setdefault("warmup_frames", 0)
    last_error = None
    for attempt in range(8):
        try:
            return FakeFerretDaemon(port=_pick_port(), **kwargs).start()
        except OSError as err:  # port raced — try the next one
            last_error = err
    raise last_error


# --- golden replay -------------------------------------------------------------
def test_golden_replay_byte_exact_through_public_api(tmp_path):
    """probe/rpc_depth.npy served over RPC decodes to EXACTLY its own bytes."""
    golden_raw = np.load(GOLDEN_NPY)
    payload = golden_y16_payload()
    assert len(payload) == Y16_FULL_BYTES == 1_024_000
    assert payload == golden_raw.astype("<u2", copy=False).tobytes()

    daemon = _start_daemon(frames=[payload])
    try:
        cam = _open_camera(daemon, tmp_path)
        depth_mm = cam.get_depth_mm()
        cam.close()
    finally:
        daemon.stop()

    # Same decode the offline tests guarantee — now through the full RPC path.
    assert np.array_equal(depth_mm, capture.decode_y16_depth_mm(payload))

    # Byte-exactness end to end: recover raw u16 from the returned millimetres.
    # The frame contains no 0xFFFF pixels (verified below), so every sentinel
    # zero in the decode is a genuine raw 0.
    recovered = np.rint(depth_mm.astype(np.float64) / 0.2).astype(np.uint16)
    assert int((golden_raw == 0xFFFF).sum()) == 0
    assert np.array_equal(recovered, golden_raw), "golden replay not byte-exact"


# --- both modes ----------------------------------------------------------------
def test_computed_y16_mode(tmp_path):
    """A synthetic computed-depth frame arrives intact through get_depth_mm."""
    payload = synthetic_depth_u16(value=1000)  # raw 1000 -> 200.0 mm
    daemon = _start_daemon(frames=[payload])
    try:
        cam = _open_camera(daemon, tmp_path)
        depth = cam.get_depth_mm()
        cam.close()
    finally:
        daemon.stop()

    assert depth.shape == (800, 640) and depth.dtype == np.float32
    assert float(depth[400, 320]) == pytest.approx(200.0)
    assert depth[0, 0] == 0.0 and depth[-1, -1] == 0.0  # sentinels -> invalid


def test_raw_y14_disparity_mode(tmp_path):
    """A raw-Y14 disparity frame converts to self-consistent metric depth."""
    rng = np.random.default_rng(3)
    true_depth = rng.uniform(150.0, 300.0, size=(800, 1280)).astype(np.float32)
    payload = synthetic_disparity(depth_mm=true_depth)
    assert len(payload) == Y14_FULL_BYTES == 1_792_000

    daemon = _start_daemon(frames=[payload])
    try:
        cam = _open_camera(daemon, tmp_path)
        depth = cam.get_depth_mm()
        cam.close()
    finally:
        daemon.stop()

    expected = capture.disparity_to_depth_mm(
        capture.unpack_y14(payload, 1280, 800))
    assert np.array_equal(depth, expected)

    valid = depth[depth > 0]
    assert valid.size > 0.9 * depth.size, "valid pixels lost in conversion"
    assert abs(float(np.median(valid)) - float(np.median(true_depth))) < 5.0, (
        "disparity->depth round trip drifted from the synthesised scene")


def test_ir_y8_mode(tmp_path):
    """get_ir returns the 1280x800 uint8 IR frame the daemon 'streamed'."""
    daemon = _start_daemon(frames=[ir_payload(77)])
    try:
        cam = _open_camera(daemon, tmp_path)
        ir = cam.get_ir()
        cam.close()
    finally:
        daemon.stop()

    assert ir.shape == (800, 1280) and ir.dtype == np.uint8
    assert int(ir.min()) == int(ir.max()) == 77


def test_warmup_empty_frames_are_polled_through(tmp_path):
    """Empty warm-up slots are skipped; the first real frame still lands."""
    daemon = _start_daemon(
        frames=[synthetic_depth_u16()], warmup_frames=3)
    try:
        cam = _open_camera(daemon, tmp_path)
        depth = cam.get_depth_mm()
        cam.close()
    finally:
        daemon.stop()
    assert float(depth[400, 320]) == pytest.approx(200.0)


# --- failure injection -----------------------------------------------------------
def test_truncated_payload_raises_clear_error(tmp_path):
    """A truncated frame must raise ValueError, never decode into garbage."""
    daemon = _start_daemon(
        frames=[golden_y16_payload()], truncate_next_frame_by=1280)
    try:
        cam = _open_camera(daemon, tmp_path)
        with pytest.raises(ValueError, match="frame size"):
            cam.get_depth_mm()
        cam.close()
    finally:
        daemon.stop()


def test_sentinel_flooded_frame_is_all_invalid(tmp_path):
    """A 0x3FFF-flooded Y14 frame decodes to all-zero depth (no bogus points)."""
    payload = flooded_sentinel_y14()
    assert len(payload) == Y14_FULL_BYTES
    daemon = _start_daemon(frames=[payload])
    try:
        cam = _open_camera(daemon, tmp_path)
        depth = cam.get_depth_mm()
        cam.close()
    finally:
        daemon.stop()
    assert np.all(depth == 0.0)


def test_mid_stream_disconnect_surfaces_connection_error(tmp_path):
    """A daemon hangup mid-poll raises ConnectionError instead of garbage."""
    daemon = _start_daemon(
        frames=[synthetic_depth_u16()], fail_after_requests=3)
    try:
        cam = _open_camera(daemon, tmp_path)
        cam.get_depth_mm()  # requests 1-3: discovery + start + this frame
        with pytest.raises(ConnectionError, match="closed"):
            cam.get_depth_mm()  # request 4: daemon hangs up mid-call
        cam.close()
    finally:
        daemon.stop()


# --- suffix discovery ------------------------------------------------------------
def test_wrong_suffix_is_rejected_with_rpclib_error(tmp_path):
    """A log naming another device leaves nothing to discover -> open fails."""
    daemon = _start_daemon(suffix="2-2-10")
    try:
        cam = capture.FerretCamera(
            host="127.0.0.1", port=daemon.bound_port,
            log_path=_daemon_log(tmp_path, usb_path="7-7-1.0"),
        )
        with pytest.raises(RpcError, match="discover"):
            cam.open()
    finally:
        daemon.stop()


def test_unknown_method_gets_rpclib_not_found_error():
    """Direct wire check: unknown method returns rpclib's not-found string."""
    daemon = _start_daemon()
    try:
        client = MsgpackRpcClient(host="127.0.0.1",
                                  port=daemon.bound_port, timeout=3.0)
        with pytest.raises(RpcError, match="could not find function"):
            client.call("ob_get_stream_profile_list9-9-99", timeout=3.0)
        client.close()
    finally:
        daemon.stop()


def test_wire_format_round_trip_raw_socket():
    """Response frames are [1, msgid, error, result] msgpack, no length prefix."""
    daemon = _start_daemon(frames=[synthetic_depth_u16()])
    try:
        sock = socket.create_connection(("127.0.0.1", daemon.bound_port),
                                        timeout=3.0)
        import msgpack

        sock.sendall(msgpack.packb(
            [0, 4242, "ob_rpc_get_frame2-2-10", []], use_bin_type=True))
        unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
        obj = None
        while obj is None:
            chunk = sock.recv(1 << 16)
            if not chunk:
                raise AssertionError("no response from fakeferret")
            unpacker.feed(chunk)
            for candidate in unpacker:
                obj = candidate
                break
        assert obj[0] == 1 and obj[1] == 4242 and obj[2] is None
        frameset = obj[3]
        assert len(frameset) == 11 and frameset[3] == 255
        slots = [x for x in frameset if isinstance(x, (bytes, bytearray))]
        assert max(len(s) for s in slots) == Y16_FULL_BYTES
        sock.close()
    finally:
        daemon.stop()
