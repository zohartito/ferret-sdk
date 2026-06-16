# CR-Scan Ferret — daemon RPC protocol (reverse-engineered, confirmed)

Everything here was confirmed on macOS against CrealityScan 4.2.2 /
`libOrbbecSDK.1.7.1.dylib` by live capture + replay. Device: CR-Scan Ferret,
VID `0x2BC5`, PID `0x069C`, SN `BC3M25101M5`.

## Architecture
- `/Library/Creality/RPCServer` — root LaunchAgent (`com.creality.RPCServer`,
  runs via passwordless `sudo`). Loads the Orbbec SDK with
  `OrbbecSDKConfig_v1.0.xml` → `<RPC><RPCServerEnable>true</RPCServerEnable>
  <RPCServerIPAddress>127.0.0.1</RPCServerIPAddress></RPC>`. It owns the USB
  device and serves it.
- Transport = **msgpack-RPC via rpclib** (`clmdep_msgpack`, `rpc::detail::dispatcher`,
  `RPCClientPort.cpp`). Raw msgpack stream, **no length prefix**.
  - Request  = `[0, msgid:u32, method:str, params:array]`
  - Response = `[1, msgid:u32, error, result]`
  - Notify   = `[2, method:str, params:array]`
- Ports: `10010` control; `10011/10012/10013` aux (NetDataStreamPort, raw 248-byte
  packets). **In practice depth/IR frames come back inline in the `get_frame`
  response on 10010** — the aux ports were not needed.
- The daemon also runs a GVCP (GigE-Vision) discovery server, which is why
  `ob_create_net_device("127.0.0.1", 10010)` *discovers* the device but then
  fails control (it speaks GigE, not rpclib). Use rpclib directly.

## Why this path (macOS)
A fresh process cannot claim the Ferret's UVC interfaces — libusb can't detach
Apple's kernel camera driver, even as root. The OEM daemon sidesteps it by
grabbing the device at login. So: don't claim USB, talk to the daemon.
(macOS AVFoundation exposes only the IR stream; depth/RGB are hidden — 8-bit only.)

## Per-device method suffix (IMPORTANT — changes on replug)
Methods are registered as `<base><suffix>`, where `<suffix>` = the depth-camera's
USB interface path with the dot removed:

| daemon log line | suffix |
|---|---|
| `2-2-1.0 \| CR-Scan Ferret Depth Camera` | `2-2-10` |
| `1-2-1.0 \| CR-Scan Ferret Depth Camera` | `1-2-10` |

Discover it at runtime: parse the most recent `… | CR-Scan Ferret Depth Camera`
line from `/Library/Creality/Log/ScannerStreamSDK.log`, drop the dot before the
interface number, and verify against the server by calling
`ob_get_stream_profile_list<suffix>` (rpclib returns
`"could not find function …"` if wrong). See `probe/ferret_rpc_depth.py`.

## Methods (suffix omitted)
- `ob_get_stream_profile_list()` → `[[0, fmt, w, h, fps], …]`
  Formats seen: **24 = Y14** (depth/disparity), **10 = Y10/Y11**, **9 = Y8** (IR),
  **5 = MJPG** (color). Resolutions: 320×200, 640×400, 800×600, 1280×800.
- `ob_rpc_start_stream(sensor, fmt, w, h, fps)` → `True`
  Sensor enum = Orbbec `OB_SENSOR_*`: IR_RIGHT=7, DEPTH=3, COLOR=2, IR=1.
  Captured GUI preview call: `[7, 9, 1280, 800, 30]` (IR Y8).
- `ob_rpc_get_frame()` → `[a, ts, seq, 255, <bytesA>, sizeA, <bytesB>, sizeB,
  <bytesC>, sizeC, idx]` — a frameset; the large slot is the image. Empty byte
  slots while the stream warms up.
- `ob_rpc_stop_stream(...)` — arity quirk (don't pass 5 args; closing the socket
  also stops the stream).
- `ob_rpc_send_data(cmd_bytes, resp_size)` — vendor register transport. Requests
  start `GM` (0x47 0x4D), responses start `RB` (0x52 0x42). Carries exposure,
  "In_scene Calibration", and the scan-mode setup that flips depth output on.
- Global: `ob_rpc_get_device_report_infomation` (exists; unexplored).

## Frame formats observed
| bytes | meaning |
|---|---|
| 1,024,000 | **computed metric depth**, 16-bit, 512,000 px (≈800×640). `mm = u16 * 0.2`. 0 and 0xFFFF = invalid. |
| 1,024,000 (0–255) | IR Y8, 1280×800 (when daemon is in IR/preview mode) |
| 1,792,000 | **raw Y14 disparity**, full 1280×800, 14-bit packed (1280·800·14/8). Needs unpack + disparity→depth. |

`get_frame` returns computed depth vs raw Y14 depending on the daemon's processing
mode, which the GUI sets via its scan-start `send_data` sequence (not by
`start_stream` alone). To lock computed-depth output, replay that setup.

## Calibration (from `ScannerStreamSDK.log`, `FerretDevice.cpp`)
```
baseline   = 149.79
focalPix   = 956.671
z0         = 657.34
dispOffset = 218
unit       = 0.5
depthValueScale = 0.2     # u16 depth * 0.2 = millimetres
depthMode  = Close_range_mode (working range ~150–300 mm)
hwD2DEnable = false        # disparity->depth done in software (FrameProcessor)
```
Pipeline blocks: `FrameUnpacker` → `Disparity2DepthConverter` (enabled).
