# ferret-sdk

Tapping a **Creality CR-Scan Ferret** 3D scanner from code (and ultimately an LLM)
on **macOS** — by riding the Orbbec daemon CrealityScan already runs, instead of
fighting the macOS USB stack.

> **Unofficial.** This project is not affiliated with or endorsed by Creality or
> Orbbec. No Creality or Orbbec binaries are included: you need your own
> CrealityScan install, which ships the daemon and SDK this client talks to.
> The code here is MIT (see [LICENSE](LICENSE)).

## TL;DR — what we learned
- The Ferret is an **Orbbec depth camera** driven by the **Orbbec SDK** (`libOrbbecSDK.1.7.1.dylib`, shipped inside CrealityScan). No proprietary capture algorithm to crack.
- CrealityScan runs a **root daemon `RPCServer`** that owns the USB device and serves it over **msgpack-RPC (rpclib)** on `127.0.0.1:10010` (control) + `10011–10013` (aux). The GUI is just an RPC client.
- On macOS a fresh process **cannot** claim the device directly (libusb can't detach Apple's UVC kernel driver, even as root). So we **speak the daemon's RPC** and let it do the USB work — no root, no USB claim.
- We pull real frames this way: IR (Y8), raw disparity (Y14), and the daemon's **computed metric depth** (16-bit, mm via scale `0.2`).

## Status
The full `ferret` package + pipeline now exist and are tested **offline** (47/47
tests pass; the live device is never touched). See [COMPLETION-REPORT.md](COMPLETION-REPORT.md)
for the per-component breakdown, the exact numbers, and what still needs hardware.

| Capability | State |
|---|---|
| Connect to daemon, enumerate device | ✅ probe proven; refactored into `ferret.capture` (live path not re-run, safety) |
| Pull IR frame (Y8) | ✅ probe proven; `FerretCamera.get_ir()` coded, decode untested on real Y8 |
| Pull **metric depth** over RPC | ✅ working — Y16 (× 0.2 mm) decode reproduces real `rpc_depth.npy` byte-exact |
| Robust to raw-Y14 mode | ✅ decode built + tested — 14-bit unpack round-trips exactly; disparity→depth self-consistent with `z0`; high sentinel `0x3FFF` rejected. ⚠️ live daemon delivery of Y14 still unconfirmed |
| `ferret.calib` — intrinsics + depth→points (meters) | ✅ 5/5 tests on real frame; 183 412-pt cloud |
| `ferret.reconstruct` — point cloud + TSDF/numpy fusion | ✅ 7/7 tests; real frame → PLY, synthetic box recovers bbox within ~2% |
| `ferret.meshpipe` — clean / decimate / Z-up / watch | ✅ 4/4 tests; 81 985→50 000 faces, 106→1 components, watertight |
| `ferret_mcp.py` — MCP server (5 tools) | ✅ 7/7 tests; `depth_stats` + `reconstruct_pointcloud` + `clean_mesh` verified offline |
| `hardware/turntable` — CAD + firmware + host | ✅ STL renders watertight (200×200×12 mm); host control 14/14 tests vs fake serial; firmware flash unverified (no board) |

**Live-device caveat:** stream switching and decoding are tested with fake RPC
transports, not reverified on hardware. `get_ir()` selects IR_RIGHT/Y8 and
`get_depth_mm()` selects DEPTH/Y14. The daemon can return raw Y14 or computed Y16
depending on its processing mode; use `FerretCamera(depth_format="Y16")` only
when that mode is independently known. The default is strict Y14. Payload length
cannot distinguish Y8 from Y16, and the RPC envelope has no verified format
metadata. Unknown lengths and multiple image slots are rejected. Actual mode
delivery and stale frames after a switch still require hardware verification.

## Quickstart

**Use Python 3.12.** The locked offline environment was verified on macOS
arm64 with Python 3.12.16 and Open3D 0.20.0 (which has a Python 3.12 wheel).
NumPy 2.5 requires Python >=3.12; Python 3.11 cannot install these dependencies.
CI also uses Python 3.12. Other Python/platform combinations are not verified.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip check
```

`requirements.txt` records direct dependency ranges; `requirements-lock.txt`
pins the complete tested environment, including transitive dependencies. Refresh
it deliberately in a clean Python 3.12 environment with `pip install -r requirements.txt`
and `pip freeze`, then rerun the offline suite and eval gate.

### Offline demo (works on a fresh clone — no scanner, no daemon)
```bash
.venv/bin/pytest -q                                        # 47 offline tests
.venv/bin/python examples/render_mesh.py \
  artifacts/motorcycle_rhino_ready.obj                     # render committed mesh
.venv/bin/python examples/e2e_offline.py                   # synthetic fresh-clone E2E
```

The public clone does **not** include `probe/rpc_depth.npy`, `rpc_raw_frame.bin`,
or the historical `.npy` outputs (they are git-ignored). Seven tests requiring
those real captures remain explicitly skipped. The E2E example uses a deterministic
synthetic sloped plane when the real frame is absent and labels that substitution;
it verifies software reproducibility, not sensor accuracy. Outputs go to the
ignored `artifacts/offline-demo/`, or a chosen `--output-dir`.

### Live capture (needs the hardware)
```bash
# CrealityScan's RPCServer daemon must be running (launch CrealityScan once)
.venv/bin/python probe/ferret_rpc_depth.py   # -> rpc_depth.npy + rpc_depth_preview.png
# For a daemon independently known to be in computed-depth mode:
.venv/bin/python probe/ferret_rpc_depth.py 3 24 Y16
```

The probe saves float32 millimetres (zero = invalid), plus the original raw
bytes. Its default Y14 mode uses the package disparity decoder; it rejects
other payload sizes instead of guessing their format.

## Layout
```
ferret/                 # the SDK package (built + tested offline)
  calib.py              # intrinsics(w,h); depth_to_points(depth_mm, intr) -> Nx3 meters
  capture.py            # FerretCamera (Y16 ×0.2 + Y14 unpack→disparity→depth); suffix auto-discovery
  rpc_client.py         # package-local copy of the msgpack-RPC client
  reconstruct.py        # depth_to_pointcloud / fuse (Open3D TSDF or numpy) / save_ply / save_obj
  meshpipe.py           # clean_mesh (largest comp, fill holes, recenter, Z-up, decimate) + watch_folder
ferret_mcp.py           # MCP server (MCPServer): depth_stats, reconstruct_pointcloud, clean_mesh, turntable_rotate, list_artifacts
hardware/turntable/     # turntable.scad + firmware/turntable.ino + turntable_control.py + BOM
tests/                  # pytest suite (47 tests, all offline)
artifacts/              # inspectable outputs: PLY / OBJ / STL / PNG
probe/                  # device investigation scripts; real raw/depth fixtures are not shipped
  ferret_rpc_depth.py   # main: pull depth via daemon RPC (auto-discovers device id)
  rpc_client.py         # minimal msgpack-RPC (rpclib) client for the daemon
ARCHITECTURE.md         # system design + platform decision
BUILD-PLAN.md           # phased plan
PROTOCOL.md             # confirmed wire protocol + calibration (the crown jewel)
COMPLETION-REPORT.md    # honest per-component build/verify report
```

## Hardware
CR-Scan Ferret · USB VID `0x2BC5` PID `0x069C` · Orbbec sensor, active-stereo
structured light · depth 1280×800 Y14 disparity → metric depth.
