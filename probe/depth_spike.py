#!/usr/bin/env python3
"""
Phase-0 spike: pull a real 16-bit depth frame off the CR-Scan Ferret by binding
Creality's own Orbbec SDK dylib via ctypes (no pip wheels, guaranteed Ferret support).

Run with CrealityScan QUIT (its root RPCServer owns the device while it streams).
"""
import ctypes as C
from ctypes import (c_void_p, c_char_p, c_int, c_uint32, c_uint16, c_bool,
                    c_float, byref, POINTER)
from pathlib import Path
import os
import struct
import zlib
import time
import numpy as np

DYLIB = "/Library/Creality/libOrbbecSDK.1.7.1.dylib"
OUT = Path(__file__).resolve().parent
OB_SENSOR_DEPTH = 3  # OBSensorType

lib = C.CDLL(DYLIB)


# ---- error-checked call wrapper -------------------------------------------
lib.ob_error_message.restype = c_char_p
lib.ob_error_message.argtypes = [c_void_p]


def sig(name, restype, argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = argtypes + [POINTER(c_void_p)]  # trailing ob_error**
    return fn


def call(fn, *args):
    err = c_void_p(0)
    res = fn(*args, byref(err))
    if err.value:
        msg = lib.ob_error_message(err)
        raise RuntimeError(f"{fn.__name__}: {msg.decode() if msg else '?'}")
    return res


# ---- bind the functions we need -------------------------------------------
ob_create_context = sig("ob_create_context", c_void_p, [])
ob_query_device_list = sig("ob_query_device_list", c_void_p, [c_void_p])
ob_dev_count = sig("ob_device_list_device_count", c_uint32, [c_void_p])
ob_dev_get = sig("ob_device_list_get_device", c_void_p, [c_void_p, c_uint32])
ob_dev_info = sig("ob_device_get_device_info", c_void_p, [c_void_p])
ob_info_name = sig("ob_device_info_name", c_char_p, [c_void_p])
ob_info_pid = sig("ob_device_info_pid", c_int, [c_void_p])
ob_info_sn = sig("ob_device_info_serial_number", c_char_p, [c_void_p])

# net-device / RPC-client attach — connect to the running RPCServer daemon
ob_create_net_device = sig("ob_create_net_device", c_void_p, [c_void_p, c_char_p, c_uint16])
ob_enable_net_enum = sig("ob_enable_net_device_enumeration", None, [c_void_p, c_bool])

ob_pipe_dev = sig("ob_create_pipeline_with_device", c_void_p, [c_void_p])
ob_pipe_profiles = sig("ob_pipeline_get_stream_profile_list", c_void_p, [c_void_p, c_int])
ob_prof_count = sig("ob_stream_profile_list_count", c_uint32, [c_void_p])
ob_prof_get = sig("ob_stream_profile_list_get_profile", c_void_p, [c_void_p, c_int])
ob_vprof_w = sig("ob_video_stream_profile_width", c_uint32, [c_void_p])
ob_vprof_h = sig("ob_video_stream_profile_height", c_uint32, [c_void_p])
ob_vprof_fps = sig("ob_video_stream_profile_fps", c_uint32, [c_void_p])
ob_prof_fmt = sig("ob_stream_profile_format", c_int, [c_void_p])

ob_create_config = sig("ob_create_config", c_void_p, [])
ob_config_enable = sig("ob_config_enable_stream", None, [c_void_p, c_void_p])
ob_pipe_start = sig("ob_pipeline_start_with_config", None, [c_void_p, c_void_p])
ob_pipe_stop = sig("ob_pipeline_stop", None, [c_void_p])
ob_wait_frameset = sig("ob_pipeline_wait_for_frameset", c_void_p, [c_void_p, c_uint32])
ob_fs_depth = sig("ob_frameset_depth_frame", c_void_p, [c_void_p])
ob_frame_w = sig("ob_video_frame_width", c_uint32, [c_void_p])
ob_frame_h = sig("ob_video_frame_height", c_uint32, [c_void_p])
ob_frame_fmt = sig("ob_frame_format", c_int, [c_void_p])
ob_frame_data = sig("ob_frame_data", c_void_p, [c_void_p])
ob_frame_size = sig("ob_frame_data_size", c_uint32, [c_void_p])
ob_depth_scale = sig("ob_depth_frame_get_value_scale", c_float, [c_void_p])

# enable SDK console logging so we can see enumeration/open failures
try:
    to_console = sig("ob_set_logger_to_console", None, [c_int])
    call(to_console, 1)  # OB_LOG_SEVERITY_INFO
except Exception as e:
    print("logger setup failed:", e)


def write_png_gray8(path, arr8):
    h, w = arr8.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(arr8[y].tobytes())

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))


def main():
    print(f"loading {DYLIB}")
    # match the OEM daemon's environment: cwd=/Library/Creality so the config's
    # relative log path resolves to /Library/Creality/Log (a failed log sink
    # appears to degrade SDK init / USB enumeration). Needs root to write there.
    # cwd to a writable dir so the config's relative log path (Log/...) works
    # WITHOUT root. With the Creality config (RPCServerEnable=true) and the
    # RPCServer daemon already running, the SDK should connect as an RPC CLIENT
    # to the daemon (which owns USB) instead of claiming the device itself.
    try:
        os.chdir(OUT)
        print("cwd ->", os.getcwd())
    except Exception as e:
        print("chdir warn:", e)

    # DEFAULT context (NO Creality XML — config makes us a SERVER, not a client)
    ctx = call(ob_create_context)
    print("context created (default, no config)")
    NET_ADDR, NET_PORT = b"127.0.0.1", 10010   # daemon RPC control port

    # PRIMARY: attach to the running RPCServer daemon as a net/RPC client
    dev = None
    try:
        dev = call(ob_create_net_device, ctx, NET_ADDR, NET_PORT)
        if dev:
            print(f"ob_create_net_device OK @ {NET_ADDR.decode()}:{NET_PORT}")
    except RuntimeError as e:
        print("ob_create_net_device failed:", e)
        dev = None

    # FALLBACK A: net-device enumeration (GVCP discovery, async)
    if not dev:
        print("falling back to net-device enumeration...")
        try:
            call(ob_enable_net_enum, ctx, True)
        except RuntimeError as e:
            print("enable_net_enum warn:", e)
        for i in range(10):
            dev_list = call(ob_query_device_list, ctx)
            if call(ob_dev_count, dev_list):
                dev = call(ob_dev_get, dev_list, 0)
                break
            time.sleep(0.5)
        if not dev:
            print("NO DEVICE via net path — pivot to msgpack-RPC fallback "
                  "(see RPC-CLIENT-PLAN.md). Ensure CrealityScan's RPCServer is running.")
            return
    info = call(ob_dev_info, dev)
    name = call(ob_info_name, info)
    pid = call(ob_info_pid, info)
    sn = call(ob_info_sn, info)
    print(f"device: {name.decode()}  PID=0x{pid:04x}  SN={sn.decode()}")

    pipe = call(ob_pipe_dev, dev)
    profiles = call(ob_pipe_profiles, pipe, OB_SENSOR_DEPTH)
    pc = call(ob_prof_count, profiles)
    print(f"depth stream profiles: {pc}")
    # pick profile 0 (SDK default ordering); read its dims for the log
    prof = call(ob_prof_get, profiles, 0)
    w, h, fps, fmt = (call(ob_vprof_w, prof), call(ob_vprof_h, prof),
                      call(ob_vprof_fps, prof), call(ob_prof_fmt, prof))
    print(f"chosen depth profile: {w}x{h}@{fps}fps fmt_enum={fmt}")

    cfg = call(ob_create_config)
    call(ob_config_enable, cfg, prof)
    call(ob_pipe_start, pipe, cfg)
    print("pipeline started; waiting for depth frames...")

    best = None
    centers = []
    for i in range(80):
        fs = ob_wait_frameset(pipe, 1500, byref(c_void_p(0)))
        if not fs:
            continue
        df = call(ob_fs_depth, fs)
        if not df:
            continue
        fw, fh = call(ob_frame_w, df), call(ob_frame_h, df)
        size = call(ob_frame_size, df)
        scale = call(ob_depth_scale, df)
        ptr = call(ob_frame_data, df)
        buf = C.string_at(ptr, size)
        depth = np.frombuffer(buf, dtype=np.uint16).reshape(fh, fw).copy()
        cmm = float(depth[fh // 2, fw // 2]) * scale
        centers.append(round(cmm, 1))
        if best is None and (depth > 0).mean() > 0.05:
            best = (depth, scale, fw, fh)
        if len(centers) >= 25:
            break

    if best is None:
        print("got framesets but no usable depth — center samples:", centers[:25])
        return

    depth, scale, fw, fh = best
    valid = depth[depth > 0]
    mm = depth.astype(np.float32) * scale
    print("\n=== DEPTH FRAME ===")
    print(f"shape={depth.shape} dtype={depth.dtype} value_scale={scale}")
    print(f"valid pixels: {(depth>0).mean()*100:.1f}%")
    print(f"raw uint16 range: {depth.min()}..{depth.max()}")
    if valid.size:
        print(f"valid depth mm: min={valid.min()*scale:.0f}  "
              f"median={np.median(valid)*scale:.0f}  max={valid.max()*scale:.0f}")
    print(f"center pixel: {mm[fh//2, fw//2]:.0f} mm")
    # 3x3 grid of depths (mm) across the frame
    ys = [fh // 4, fh // 2, 3 * fh // 4]
    xs = [fw // 4, fw // 2, 3 * fw // 4]
    print("3x3 depth grid (mm):")
    for y in ys:
        print("  " + "  ".join(f"{mm[y,x]:5.0f}" for x in xs))
    print(f"\ncenter-pixel mm over {len(centers)} frames (move an object to see it change):")
    print(" ", centers)

    # save artifacts
    np.save(OUT / "depth_raw.npy", depth)
    # 16-bit true-depth PNG (big-endian)
    with open(OUT / "depth_16bit.png", "wb") as f:
        h2, w2 = depth.shape
        raw = bytearray()
        be = depth.astype(">u2")
        for y in range(h2):
            raw.append(0)
            raw.extend(be[y].tobytes())

        def chunk(typ, data):
            return (struct.pack(">I", len(data)) + typ + data
                    + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w2, h2, 16, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))
    # normalized 8-bit preview
    if valid.size:
        lo, hi = np.percentile(valid, 2) * scale, np.percentile(valid, 98) * scale
        norm = np.clip((mm - lo) / max(hi - lo, 1e-6), 0, 1)
        prev = (norm * 255).astype(np.uint8)
        prev[depth == 0] = 0
        write_png_gray8(OUT / "depth_preview8.png", prev)
    print(f"\nsaved: depth_raw.npy, depth_16bit.png, depth_preview8.png in {OUT}")

    with open(OUT / "results.txt", "w") as f:
        f.write(f"device: {name.decode()} PID=0x{pid:04x} SN={sn.decode()}\n")
        f.write(f"depth shape={depth.shape} dtype={depth.dtype} value_scale={scale}\n")
        f.write(f"valid_pct={(depth>0).mean()*100:.1f}\n")
        if valid.size:
            f.write(f"valid_mm min={valid.min()*scale:.0f} "
                    f"median={np.median(valid)*scale:.0f} max={valid.max()*scale:.0f}\n")
        f.write(f"center_mm={mm[fh//2, fw//2]:.0f}\n")
        f.write(f"centers_over_frames={centers}\n")

    call(ob_pipe_stop, pipe)


if __name__ == "__main__":
    main()
