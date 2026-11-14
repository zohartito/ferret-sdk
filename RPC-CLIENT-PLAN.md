# Ride the running RPCServer daemon to get a macOS-native depth frame

**Goal:** A fresh Python (ctypes) process connects to the ALREADY-RUNNING root
`/Library/Creality/RPCServer` daemon **as a CLIENT** and streams depth frames
from the CR-Scan Ferret — without claiming USB directly (macOS blocks USB detach
for a fresh process; the daemon owns USB and we ride it over loopback TCP).

**Root cause of the earlier failure (settled):** `RPCServerEnable=true` in the XML
makes a process a *server*, not a client. `MacPal::MacPal()` reads
`Device.RPC.RPCServerEnable`; if true it builds an `RPCServerManager` and never an
`RPCClientPort`. A second process loading that config tries to become a *second*
server, finds 0 USB devices (the daemon owns USB), and never touches the network
stack. **Becoming a client is an explicit API call — `ob_create_net_device` — not
a config flag.**

**Live ground truth (re-verified this session):**
- Daemon `RPCServer` (root, pid 63847, launched via `/usr/bin/sudo`) LISTENs on
  `127.0.0.1:10010,10011,10012,10013`.
- CrealityScan GUI (the reference *client*) holds ESTABLISHED conns
  `->10010` (control), `->10011`, `->10012` (stream).
- All four net exports exist in `/Library/Creality/libOrbbecSDK.1.7.1.dylib`:
  `_ob_create_net_device@0x437d74`, `_ob_enable_net_device_enumeration@0x437a0c`,
  `_ob_enable_network_server@0x438284`, `_ob_disable_network_server@0x438580`.
- Rejection branch string present: `Create Net Device failed, unsupported device!
  address={0}, port={1}, pid=0x{2:04x}` — watch for this.

---

## RECOMMENDED PRIMARY: SDK C-API client via `ob_create_net_device`

Fastest path because it reuses the entire working pipeline/profile/frameset chain
in `depth_spike.py` (lines 155-253) verbatim. The only new work is binding three
exports and calling `ob_create_net_device` on a **default** context (no Creality
config) to get an `ob_device*` served by the daemon.

### Exact code to ADD to `/path/to/ferret-sdk/probe/depth_spike.py`

Add `c_uint16, c_bool` to the ctypes import on line 9:

```python
from ctypes import (c_void_p, c_char_p, c_int, c_uint32, c_uint16, c_bool,
                    c_float, byref, POINTER)
```

Add the net bindings next to the other `sig(...)` calls (after line 53):

```python
# ---- net-device (RPC CLIENT) bindings -------------------------------------
# Engage the SDK as an RPC CLIENT to the running daemon. ob_create_net_device
# -> DeviceManager::createNetDevice -> NetDeviceEnumerator::createDevice ->
# FerretUvcDevice; its source ports become RPCClientPort TCP clients to
# 127.0.0.1:10010 (control) + negotiated 10011/10012/10013 (stream).
ob_create_net_device = sig("ob_create_net_device", c_void_p,
                           [c_void_p, c_char_p, c_uint16])
ob_enable_net_enum = sig("ob_enable_net_device_enumeration", None,
                         [c_void_p, c_bool])
```

Replace the context-creation + enumeration block (`depth_spike.py` lines 119-153,
the `ctx = None ... print(f"device: ...")` span) with this. It creates a
**default** context (no RPCServerEnable config → you do NOT become a competing
server), attaches via `ob_create_net_device` (primary), and falls back to
enumeration if that returns NULL:

```python
    # DEFAULT context only — do NOT load the Creality XML (RPCServerEnable=true
    # would make THIS process a second server and collide on 10010-10013).
    ctx = call(ob_create_context)
    print("context created (default, no config)")

    NET_ADDR = b"127.0.0.1"
    NET_PORT = 10010  # control port the daemon GVCP-advertises the Ferret on

    dev = None
    # --- PRIMARY: direct net-device attach (skips flaky loopback GVCP UDP) ---
    try:
        dev = call(ob_create_net_device, ctx, NET_ADDR, NET_PORT)
        if dev:
            print(f"ob_create_net_device OK -> device @ {NET_ADDR.decode()}:{NET_PORT}")
    except RuntimeError as e:
        # e.g. "Create Net Device failed, unsupported device! ... pid=0x069c"
        print("ob_create_net_device failed:", e)
        dev = None

    # --- FALLBACK A: enumeration over the net (GVCP discovery, async ~2-5s) ---
    if not dev:
        print("falling back to net-device enumeration...")
        try:
            call(ob_enable_net_enum, ctx, True)
        except RuntimeError as e:
            print("enable_net_enum warn:", e)
        n = 0
        for i in range(10):  # ~5s; GVCP discovery is async over loopback
            dev_list = call(ob_query_device_list, ctx)
            n = call(ob_dev_count, dev_list)
            if n:
                dev = call(ob_dev_get, dev_list, 0)
                print(f"enumeration found {n} net device(s)")
                break
            time.sleep(0.5)
        if not dev:
            print("NO DEVICE via net-device path. The stock createNetDevice flow "
                  "is FemtoMega-oriented and may reject the Ferret PID 0x069C. "
                  "Pivot to the msgpack-RPC fallback (see RPC-CLIENT-PLAN.md).")
            return

    info = call(ob_dev_info, dev)
    name = call(ob_info_name, info)
    pid = call(ob_info_pid, info)
    sn = call(ob_info_sn, info)
    print(f"device: {name.decode()}  PID=0x{pid:04x}  SN={sn.decode()}")
```

Everything from `pipe = call(ob_pipe_dev, dev)` (line 155) onward is reused
unchanged: `ob_create_pipeline_with_device` -> `ob_pipeline_get_stream_profile_list(OB_SENSOR_DEPTH)`
-> pick profile -> `ob_create_config` -> `ob_config_enable_stream` ->
`ob_pipeline_start_with_config` -> `ob_pipeline_wait_for_frameset` ->
`ob_frameset_depth_frame`. Streaming flows over 10011/10012/10013 transparently
via the RPCClientPort source ports.

### Run

```bash
# Leave the daemon running. Try WITHOUT root first (loopback TCP needs no root):
/opt/homebrew/bin/python3 /path/to/ferret-sdk/probe/depth_spike.py
```

### Success criterion

`=== DEPTH FRAME ===` prints with `shape=(800, 1280)`, non-zero valid-pixel %,
and the `center-pixel mm over N frames` list changes when an object moves in front
of the camera. Artifacts written: `depth_raw.npy`, `depth_16bit.png`,
`depth_preview8.png`, `results.txt`.

### Hard rules

- Do **NOT** call `ob_enable_network_server` / `ob_disable_network_server` — those
  are SERVER-side and will collide with the daemon on 10010-10013.
- Do **NOT** load `OrbbecSDKConfig_v1.0.xml` (its `RPCServerEnable=true` makes you a
  server). Use the default context.
- The daemon advertises `multi-client mode`, so you can connect alongside the GUI.
  If a stream conflict appears, QUIT the GUI but leave the daemon running, then retry.

### Known risk

`createNetDevice` is FemtoMega-oriented (success log literal `FemtoMega Net device
created!`). If `ob_create_net_device` returns NULL with `unsupported device!
pid=0x069c`, the stock net path does not accept the USB Ferret. Do not retry —
go straight to the msgpack-RPC fallback.

---

## FALLBACK: speak msgpack-RPC (rpclib) to the daemon directly

Viable and partially proven live: a 2nd Python client connected to `127.0.0.1:10010`
while the GUI streamed and got a textbook rpclib response
(`[1, 1, "rpclib: server could not find function 'getDeviceInfo' with argument
count 0.", null]`). This bypasses the FemtoMega/unsupported-PID gate entirely
because it speaks the same wire protocol the GUI uses.

**Architecture split (important):**
- **Control = `10010`**, msgpack-RPC (rpclib). Raw msgpack stream, **no** length
  prefix. `Request=[0,msgid,method,params]`, `Response=[1,msgid,err,result]`,
  `Notify=[2,method,params]`.
- **Stream = `10011/10012/10013`**, NOT msgpack. A separate raw binary transport
  (`NetDataStreamPort`): fixed **248-byte (0xf8)** packets, magic `0x4321` + CRC +
  frameId/index header. Reassemble payload chunks to `1280*800*2 = 2,048,000` bytes,
  then `np.frombuffer(buf, '<u2').reshape(800, 1280)`.

Exact arities/param types for `startStream`/`getDeviceInfo` etc. and the 248-byte
header field offsets are NOT string-exposed — they must come from a live capture of
the GUI's traffic.

### Step 1 — install msgpack (PEP 668 on Homebrew Python 3.14)

```bash
/opt/homebrew/bin/python3 -m pip install --user --break-system-packages msgpack
```

### Step 2 — capture the GUI handshake (NEEDS ROOT for tcpdump)

```bash
sudo /usr/sbin/tcpdump -i lo0 -s 0 -w /tmp/rpc.pcap \
  '(tcp port 10010 or tcp port 10011 or tcp port 10012 or tcp port 10013)'
# then in CrealityScan start a scan/preview so depth streams ~3-5s, Ctrl-C tcpdump
```

### Step 3 — decode the capture (stdlib-only decoder already written)

```bash
/opt/homebrew/bin/python3 /path/to/ferret-sdk/probe/decode_rpc_pcap.py /tmp/rpc.pcap
```

Read off: the exact 10010 call sequence (method names + param arrays + result
shapes — gives `getDeviceInfo`/`startStream` arity and types), and WHICH data port
(10011/12/13) carried depth (correlate with `startStream` params).

### Step 4 — replay control + reassemble pixels

Using `/path/to/ferret-sdk/probe/rpc_client.py` (runs end-to-end):

1. Hardcode the captured control sequence on `10010`:
   `getProtocolVersion`/`getCmdVersion` (handshake) -> `getDeviceInfo` (confirm PID
   0x069C) -> `getStreamProfileList` -> pick 1280x800 depth -> `getCameraParamWithProfile`
   -> `startStream(<depth profile params, byte-for-byte from capture>)`.
2. Open a second socket on the depth data port; read exactly 248 bytes per packet
   in a recv-loop; reverse the header (magic `0x4321` offset, payload-size, frameId,
   packetIndex); reassemble payload slices keyed by frameId until total ==
   2,048,000 bytes; `np.frombuffer('<u2').reshape(800, 1280)`.
3. Convert disparity -> metric depth with the known calibration
   (baseline 149.79, focalPix 956.671, z0 657.34, dispOffset 218, scale ~0.2) OR
   fetch live via `getCameraParam` over RPC.
4. Sanity check: center-pixel mm tracks a moving object (same check as the spike).

**Purely-passive variant (lowest risk):** if starting a second stream conflicts at
the daemon, keep the GUI streaming and just open a read-only socket to the active
data port to sniff the 248-byte packets the daemon is already emitting — no
`startStream` needed.

### Confirmed RPC control method names

`getDeviceInfo, getStreamProfileList, startStream, stopStream, getCameraParam,
getCameraParamWithProfile, getCameraParamListFromFM, getProperty, getPropertyValue,
setProperty, setPropertyValue, getValueScale, getProtocolVersion, getCmdVersion,
sendData, recvData, getFrame`.

---

## Root requirement

- **Primary (C-API net-device):** NO root. Loopback TCP to 10010 needs no
  privileges. The daemon already runs as root and owns USB.
- **Fallback control replay:** NO root for the Python client.
- **Fallback capture only:** `tcpdump -i lo0` NEEDS root (one-time, to learn the
  handshake). After the params are known, replay runs unprivileged.

## Relevant files

- `/path/to/ferret-sdk/probe/depth_spike.py` — working C-API
  ctypes binding; reused verbatim from the pipeline step onward.
- `/path/to/ferret-sdk/probe/rpc_client.py` — msgpack-RPC client
  skeleton (connect/request/parse, runs).
- `/path/to/ferret-sdk/probe/decode_rpc_pcap.py` — stdlib pcap
  decoder (BSD-loopback + IPv4/TCP reassembly, msgpack on 10010, 248B slicing on
  10011-13, 0x4321 hunt).
- `/Library/Creality/libOrbbecSDK.1.7.1.dylib` — SDK (net exports confirmed).
- `/Library/Creality/OrbbecSDKConfig_v1.0.xml` — do NOT load (server flag).
