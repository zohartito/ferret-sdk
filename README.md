# ferret-sdk

Tapping a **Creality CR-Scan Ferret** 3D scanner from code (and ultimately an LLM)
on **macOS** — by riding the Orbbec daemon CrealityScan already runs, instead of
fighting the macOS USB stack.

## TL;DR — what we learned
- The Ferret is an **Orbbec depth camera** driven by the **Orbbec SDK** (`libOrbbecSDK.1.7.1.dylib`, shipped inside CrealityScan). No proprietary capture algorithm to crack.
- CrealityScan runs a **root daemon `RPCServer`** that owns the USB device and serves it over **msgpack-RPC (rpclib)** on `127.0.0.1:10010` (control) + `10011–10013` (aux). The GUI is just an RPC client.
- On macOS a fresh process **cannot** claim the device directly (libusb can't detach Apple's UVC kernel driver, even as root). So we **speak the daemon's RPC** and let it do the USB work — no root, no USB claim.
- We pull real frames this way: IR (Y8), raw disparity (Y14), and the daemon's **computed metric depth** (16-bit, mm via scale `0.2`).

## Status
| Capability | State |
|---|---|
| Connect to daemon, enumerate device | ✅ working, replug-robust (auto-discovers device id) |
| Pull IR frame (Y8) | ✅ |
| Pull metric depth frame | ✅ proven (center 308 mm, ~94% valid) |
| Reliable depth every run regardless of daemon mode | ⚠️ last mile — see below |
| Reconstruction (depth → mesh), MCP server, turntable | ⬜ planned |

**Last mile:** `get_frame` returns raw Y14 *or* computed depth depending on the
daemon's processing mode, which the GUI sets via its scan-start `send_data` setup.
Two ways to lock it: (A) replay that setup from one real-scan capture, or
(B) unpack Y14 + convert with the calibration in [PROTOCOL.md](PROTOCOL.md).

## Quickstart
```bash
pip install -r requirements.txt          # numpy, msgpack
# CrealityScan's RPCServer daemon must be running (launch CrealityScan once)
python3 probe/ferret_rpc_depth.py        # -> rpc_depth.npy + rpc_depth_preview.png
```

## Layout
```
probe/
  ferret_rpc_depth.py   # main: pull depth via daemon RPC (auto-discovers device id)
  rpc_client.py         # minimal msgpack-RPC (rpclib) client for the daemon
  rpc_view.py           # render a frame multiple ways (format/resolution ID)
  decode_rpc_pcap.py    # decode a tcpdump capture of the GUI<->daemon traffic
  depth_spike.py        # ctypes binding to libOrbbecSDK (USB + net-device paths)
  grab_ir.sh            # IR grab via ffmpeg/AVFoundation (macOS exposes IR only)
ARCHITECTURE.md         # system design + platform decision
BUILD-PLAN.md           # phased plan
RPC-CLIENT-PLAN.md      # the RPC-client investigation that cracked the protocol
PROTOCOL.md             # confirmed wire protocol + calibration (the crown jewel)
```

## Hardware
CR-Scan Ferret · USB VID `0x2BC5` PID `0x069C` · Orbbec sensor, active-stereo
structured light · depth 1280×800 Y14 disparity → metric depth.
