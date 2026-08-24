"""fakeferret — an offline simulator of the Creality/Orbbec RPCServer daemon.

Speaks the rpclib msgpack-RPC wire format (PROTOCOL.md) on 127.0.0.1:20010+ so
``ferret.capture.FerretCamera`` can be exercised end-to-end without the real
daemon, the USB device, or ports 10010-10013.

Wire protocol (no length prefix):
    Request  = [0, msgid:u32, method:str, params:array]
    Response = [1, msgid:u32, error, result]
    Notify   = [2, method:str, params:array]

Methods are registered as ``<base><suffix>`` like the real daemon, dispatch is
by ``(method_name, argument_count)``, and unknown methods return the same
"could not find function" rpclib error string suffix discovery relies on.
"""
from .server import DEFAULT_HOST, DEFAULT_PORT, FakeFerretDaemon
from .frames import (
    DEPTH_H,
    DEPTH_W,
    NATIVE_H,
    NATIVE_W,
    Y14_FULL_BYTES,
    Y16_FULL_BYTES,
    flooded_sentinel_y14,
    golden_y16_payload,
    ir_payload,
    synthetic_depth_u16,
    synthetic_disparity,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEPTH_H",
    "DEPTH_W",
    "FakeFerretDaemon",
    "NATIVE_H",
    "NATIVE_W",
    "Y14_FULL_BYTES",
    "Y16_FULL_BYTES",
    "flooded_sentinel_y14",
    "golden_y16_payload",
    "ir_payload",
    "synthetic_depth_u16",
    "synthetic_disparity",
]
