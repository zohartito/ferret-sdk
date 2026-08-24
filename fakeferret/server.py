"""The fakeferret RPCServer daemon — a real msgpack-RPC (rpclib-compatible)
server that mimics the Creality/Orbbec daemon's control channel.

Listens on 127.0.0.1:20010+ ONLY (the real daemon on 10010-10013 is never
contacted). Implements exactly the per-device methods FerretCamera calls:

    ob_get_stream_profile_list<suffix>()            -> [[0, fmt, w, h, fps], ...]
    ob_rpc_start_stream<suffix>(sensor, fmt, w, h, fps) -> True
    ob_rpc_get_frame<suffix>()                      -> frameset (see below)
    ob_rpc_stop_stream<suffix>(...)                 -> True

frameset = [a, ts_ms, seq, 255, <bytesA>, sizeA, <bytesB>, sizeB,
            <bytesC>, sizeC, idx]   (PROTOCOL.md; the big slot is the image)

Dispatch is by (method_name, argument_count) like rpclib; unknown methods or
arities return "rpclib: server could not find function '<m>' with argument
count <n>." — the error string suffix discovery keys on.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Optional, Union

import msgpack

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 20010          # everything fakeferret serves lives on 20010+

TYPE_REQUEST, TYPE_RESPONSE, TYPE_NOTIFY = 0, 1, 2

# Orbbec enums mirrored from ferret.capture / PROTOCOL.md.
OB_SENSOR_DEPTH = 3
OB_FORMAT_Y8 = 9
OB_FORMAT_Y14 = 24


FrameProvider = Callable[[int], bytes]


def _profiles_for(fmt: int) -> list:
    """Stream profile list shape: [[index, fmt, w, h, fps], ...]."""
    return [[0, int(fmt), 1280, 800, 30]]


class FakeFerretDaemon:
    """Offline RPCServer simulator.

    Args:
        frames: the payload(s) to serve, in order; served round-robin when
            ``get_frame`` is called more times than there are frames. Either a
            list of byte payloads or a callable ``(frame_index) -> bytes``.
        warmup_frames: number of get_frame calls answered with empty byte
            slots first, mimicking the daemon warming up its stream.
        fail_after_requests: if set, hard-close the client connection after
            serving this many requests without responding (mid-stream
            disconnect injection).
        truncate_next_frame_by: if > 0, cut that many bytes off the NEXT
            frame payload once (truncated-payload injection).

    Use as a context manager (``with FakeFerretDaemon(...) as daemon:``) which
    starts and stops the listener thread; ``daemon.bound_port`` gives the
    actual port to point FerretCamera at.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        suffix: str = "2-2-10",
        frames: Union[list[bytes], FrameProvider, None] = None,
        warmup_frames: int = 1,
        fail_after_requests: Optional[int] = None,
        truncate_next_frame_by: int = 0,
        start_stream_fmt: int = OB_FORMAT_Y14,
    ):
        self.host = host
        self.port = port
        self.suffix = suffix
        self.warmup_frames = warmup_frames
        self.fail_after_requests = fail_after_requests
        self.truncate_next_frame_by = truncate_next_frame_by
        self.start_stream_fmt = start_stream_fmt
        self._frames = frames if frames is not None else []
        self._listen_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._served = 0          # get_frame calls actually carrying data
        self._requests = 0        # all dispatched requests
        self._truncation_spent = False
        self.requests_served = 0  # observability for tests
        self.stopped_streams = 0
        self.started_streams = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> "FakeFerretDaemon":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(4)
        sock.settimeout(0.2)
        self._listen_sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(
            target=self._accept_loop, name="fakeferret", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._listen_sock is not None:
            try:
                self._listen_sock.close()
            except OSError:
                pass
            self._listen_sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def bound_port(self) -> int:
        return self.port

    def __enter__(self) -> "FakeFerretDaemon":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- connection handling --------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listen_sock.accept()
            except (TimeoutError, OSError):
                continue  # timeout tick or listener closed by stop()
            try:
                self._serve_connection(conn)
            except (ConnectionError, OSError):
                pass  # client vanished (or we hung up on purpose)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _serve_connection(self, conn: socket.socket) -> None:
        """Handle one client until it disconnects. The real daemon is a
        request/response server on one socket; so is this."""
        unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
        conn.settimeout(8.0)
        while not self._stop.is_set():
            try:
                chunk = conn.recv(1 << 16)
            except (TimeoutError, OSError):
                continue
            if not chunk:
                return  # client closed (e.g. FerretCamera.close())
            unpacker.feed(chunk)
            for obj in unpacker:
                response = self._dispatch(obj)
                if response is None:
                    continue  # unparseable/noise: real daemon would too
                if self.fail_after_requests is not None and (
                    self._requests > self.fail_after_requests
                ):
                    conn.close()  # mid-stream disconnect: no response at all
                    return
                conn.sendall(msgpack.packb(response, use_bin_type=True))

    # -- dispatch -------------------------------------------------------------
    def _dispatch(self, obj) -> Optional[list]:
        if not isinstance(obj, (list, tuple)) or len(obj) < 4:
            return None
        kind = obj[0]
        if kind == TYPE_NOTIFY:
            return None  # fire-and-forget: nothing to answer
        if kind != TYPE_REQUEST:
            return None
        _kind, msgid, method, params = obj[0], obj[1], obj[2], obj[3]
        with self._lock:
            self._requests += 1
            self.requests_served += 1
        handler = self._route(method, len(params))
        if handler is None:
            return [
                TYPE_RESPONSE,
                msgid,
                f"rpclib: server could not find function '{method}' with "
                f"argument count {len(params)}.",
                None,
            ]
        error, result = handler(params)
        return [TYPE_RESPONSE, msgid, error, result]

    def _route(self, method: str, argc: int):
        """rpclib-style (name, argument_count) routing over suffixed methods."""
        table = {
            ("ob_get_stream_profile_list" + self.suffix, 0): self._on_profiles,
            ("ob_rpc_start_stream" + self.suffix, 5): self._on_start_stream,
            ("ob_rpc_stop_stream" + self.suffix, 5): self._on_stop_stream,
            ("ob_rpc_stop_stream" + self.suffix, 0): self._on_stop_stream,
            ("ob_rpc_get_frame" + self.suffix, 0): self._on_get_frame,
        }
        return table.get((method, argc))

    # -- method handlers ------------------------------------------------------
    def _on_profiles(self, _params):
        return None, _profiles_for(self.start_stream_fmt)

    def _on_start_stream(self, params):
        sensor, fmt, _w, _h, _fps = params
        del sensor
        self.start_stream_fmt = int(fmt)
        self.started_streams += 1
        with self._lock:
            self._served = -self.warmup_frames  # warm-up before real frames
        return None, True

    def _on_stop_stream(self, _params):
        self.stopped_streams += 1
        return None, True

    def _next_payload(self) -> bytes:
        index = max(self._served, 0)
        if callable(self._frames):
            payload = bytes(self._frames(index))
        elif isinstance(self._frames, list) and self._frames:
            payload = bytes(self._frames[index % len(self._frames)])
        else:
            payload = b""
        with self._lock:
            self._served += 1
        cut = self.truncate_next_frame_by
        if cut > 0 and not self._truncation_spent:
            self._truncation_spent = True
            payload = payload[:-cut] if cut < len(payload) else b""
        return payload

    def _on_get_frame(self, _params):
        with self._lock:
            if self._served < 0:
                self._served += 1  # burn one warm-up slot per empty answer
                seq = None
            else:
                seq = self._served
        if seq is None:
            # Stream still warming up: every byte slot empty, like the daemon.
            return None, [1, int(time.time() * 1000), 0, 255,
                          b"", 0, b"", 0, b"", 0, 0]
        payload = self._next_payload()
        return None, [
            1,                       # 'a' — frameset tag seen on the wire
            int(time.time() * 1000),  # ts
            seq,                     # sequence
            255,                     # constant observed in captures
            payload, len(payload),   # slot A: the image
            b"", 0,                  # slot B: empty
            b"", 0,                  # slot C: empty
            seq,                     # idx
        ]
