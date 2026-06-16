#!/usr/bin/env python3
"""
Fallback path: talk msgpack-RPC (rpclib wire format) DIRECTLY to the already-running
Creality/Orbbec RPCServer daemon, bypassing the SDK C-API entirely.

VERIFIED against the live daemon this session:
  - 127.0.0.1:10010 accepts a SECOND client concurrently with the CrealityScan GUI.
  - Wire format is rpclib msgpack-RPC: raw msgpack stream, NO length prefix.
    Request  = [0, msgid:uint32, method:str, params:array]
    Response = [1, msgid:uint32, error, result]
    Notify   = [2, method:str, params:array]
  - rpclib dispatches by (method_name, argument_count); wrong arity returns:
      "rpclib: server could not find function '<m>' with argument count <n>."

ARCHITECTURE (from dylib symbol/disasm inspection):
  - Port 10010 = msgpack-RPC CONTROL channel (RPCClientPort).
  - Ports 10011/10012/10013 = RAW framed data streams (NetDataStreamPort), NOT msgpack.
    NetDataStreamPort::readData() reads a FIXED 248-byte (0xf8) packet via
    VendorTCPClient::read(buf, 0xf8) and dispatches buf to RawDataStreamWatcher
    callbacks. Frame packets carry a binary header with a magic word (expected
    0x4321 seen for "Direction key data"; generic "Frame with invalid magic
    number" check elsewhere) + CRC + sequencing. Exact offsets come from capture.

Control method names present in the SDK: getDeviceInfo, getStreamProfileList,
startStream, stopStream, getCameraParam, getCameraParamWithProfile,
getCameraParamListFromFM, getProperty/getPropertyValue,
setProperty/setPropertyValue, sendData, recvData, getFrame.
"""
import socket
import time
import msgpack

TYPE_REQUEST, TYPE_RESPONSE, TYPE_NOTIFY = 0, 1, 2


class RpcError(RuntimeError):
    pass


class MsgpackRpcClient:
    """Minimal rpclib-compatible client: raw msgpack stream, no length prefix."""

    def __init__(self, host="127.0.0.1", port=10010, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        # raw=False -> decode msgpack str as Python str; bytes stay bytes.
        self._unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
        self._msgid = 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _next_obj(self, timeout):
        """Pull the next complete msgpack object off the streaming socket."""
        deadline = time.time() + timeout
        while True:
            for obj in self._unpacker:
                return obj
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("no msgpack object within timeout")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            self._unpacker.feed(chunk)

    def call(self, method, *params, timeout=5.0):
        """Synchronous request/response. Returns result, raises RpcError on error."""
        self._msgid = (self._msgid + 1) & 0xFFFFFFFF
        mid = self._msgid
        req = msgpack.packb([TYPE_REQUEST, mid, method, list(params)],
                            use_bin_type=True)
        self.sock.sendall(req)
        # rpclib responses come back in order on this socket; match on msgid and
        # skip any notifications (type 2) the server may interleave.
        while True:
            obj = self._next_obj(timeout)
            if not isinstance(obj, (list, tuple)):
                continue
            if obj and obj[0] == TYPE_NOTIFY:
                continue  # async notification; ignore for sync call
            if len(obj) >= 4 and obj[0] == TYPE_RESPONSE and obj[1] == mid:
                _, _, err, result = obj[0], obj[1], obj[2], obj[3]
                if err is not None:
                    raise RpcError(err)
                return result
            # otherwise keep reading (stale/out-of-order)


class NetDataStream:
    """Raw 248-byte-packet reader for the frame data ports (10011/10012/10013).

    NOT msgpack. NetDataStreamPort::readData() loops reading exactly 0xf8 bytes
    per packet, then dispatches the whole 248-byte buffer to watchers. Full-frame
    reassembly (concatenating payload slices keyed by a frame id / packet index in
    the header) happens above that. The header layout (magic/size/frameId/offset)
    must be confirmed from a live capture before this can reassemble a frame.
    """
    PACKET_SIZE = 0xF8  # 248 bytes, confirmed from readData() disassembly

    def __init__(self, host="127.0.0.1", port=10011, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

    def read_packet(self):
        """Read exactly one 248-byte transport packet (header + chunk)."""
        buf = bytearray()
        while len(buf) < self.PACKET_SIZE:
            chunk = self.sock.recv(self.PACKET_SIZE - len(buf))
            if not chunk:
                raise ConnectionError("data stream closed")
            buf += chunk
        return bytes(buf)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    # Read-only demonstration: connect to the control port and confirm the daemon
    # is alive and speaking rpclib. Probe arity for getDeviceInfo (does NOT start
    # or stop any stream, so it will not disturb the GUI's active session).
    c = MsgpackRpcClient(port=10010)
    print("connected to control port 10010")
    for n in range(0, 6):
        try:
            res = c.call("getDeviceInfo", *([0] * n), timeout=2.0)
            print(f"getDeviceInfo with {n} args -> result: {res!r}")
            break
        except RpcError as e:
            if "argument count" in str(e):
                print(f"  {n} args: wrong arity")
            else:
                print(f"  {n} args: server error: {e}")
                break
        except TimeoutError:
            print(f"  {n} args: timeout")
            break
    c.close()
