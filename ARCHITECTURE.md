# ferret-sdk — Architecture

An agentic capture → reconstruction → post-processing pipeline for the Creality
**CR-Scan Ferret Pro**, plus an optional 3D-printed (later motorized) turntable.
**Updated platform decision (2026-11-15, FER-12):** macOS capture uses the
CrealityScan Orbbec daemon's local msgpack-RPC interface. The earlier direct-USB
investigation below is historical and superseded for capture-host selection;
its AVFoundation/libusb limitations do not rule out daemon-based depth capture.
See [README.md](README.md), [PROTOCOL.md](PROTOCOL.md), and the current §4.

---

## 1. Overview & Goal

Two user goals, in priority order:

1. **"Tap the scanner with an LLM"** — an agent-orchestrated pipeline that turns a
   physical object into a clean, Rhino-ready, Bambu-printable mesh, cataloged into
   the Obsidian vault, with an LLM/VLM only where it earns its place (natural-language
   command surface + a visual judge that grades coverage and decides where to re-scan).
2. **A 3D-printed turntable** (passive first, motorized later) that makes capture
   deterministic and registration trivial.

**The single most important architectural fact:** ~90% of this pipeline is
deterministic Python (mesh repair, coverage metrics, turntable control, export,
vault cataloging). The LLM's genuine role is narrow. We design around that honestly
rather than bolting an LLM onto problems that have exact answers.

---

## 2. Confirmed Hardware Facts (ground truth — do not re-derive)

- **Device:** Creality CR-Scan Ferret family (user owns "Ferret Pro"). USB **VID 0x2BC5, PID 0x069C**, USB 3.0 SuperSpeed.
- **Composite UVC device, 4 functions:** "Depth Camera", "IR Camera", "RGB Camera" (all `bInterfaceClass=14` UVC video) + a separate **"IMU"** interface.
- **IR native modes (confirmed via ffmpeg/avfoundation):** 320x200, 640x400, 1280x800 @ up to 30fps. Pixel formats offered: `uyvy422, yuyv422, nv12, 0rgb, bgr0` (all 8-bit). Single 1280x800 mono global-shutter IR sensor (OV9282-class).
- **It is an ACTIVE-STEREO / STRUCTURED-LIGHT camera.** A captured raw IR frame (`probe/ir_still.png`, this session) shows the projector's pseudo-random IR speckle/dot pattern on surfaces against a black background. The IR projector is ON during a plain UVC stream grab.
- **Depth is almost certainly computed ONBOARD** (a dedicated "Depth Camera" UVC function distinct from IR, plus reviews confirming an onboard depth ASIC at up to 30fps), likely streamed as a 16-bit depth map (Z16/Y16-like).
- **macOS surfaces ONLY the IR stream.** AVFoundation/CoreMediaIO hides the Depth (16-bit) and RGB streams (non-8-bit/proprietary formats). This is **confirmed** by direct probing this session AND by the platform verification below.
- **No public Creality SDK / API exists** for this device. CrealityScan is a manual Windows GUI app with no CLI/headless/batch mode — it only exports `.asc/.ply/.stl/.obj` by hand.

### What the verifications changed about our assumptions

- **REFUTED (conf 0.72):** "the 16-bit depth stream is a *standard, directly-usable* UVC format capturable by generic libuvc/V4L2 with no vendor work." The *premise* (onboard depth → a real 16-bit map) survives; the *operative claim* (plug-and-play) does not. Zero community capture projects exist across the entire Creality scanner line, the device needs a firmware/init handshake, and the closest analogues (Orbbec Astra Pro, RealSense) all require a vendor extension-unit handshake to start depth. **Treat raw depth capture as unverified, high-risk, and gated behind a vendor XU enable command.**
- **CONFIRMED (conf 0.9):** macOS `UVCAssistant`/`_cmiodalassistants` holds the UVC interfaces for exclusive access; libuvc/libusb can only break that with `sudo` or the effectively-unobtainable `com.apple.vm.device-access` entitlement, and capture is whole-device, not per-interface. **macOS is IR-only. Build on Windows/WSL2.**
- **LIKELY but CONDITIONAL (conf 0.62):** a DIY Open3D reconstruction can be "good enough" for an architecture student's uses (study models, massing, scaled prints) — **only** on the Windows/WSL2 path where depth+RGB are actually reachable, and realistically only with marker/texture assistance + a known-angle turntable for the featureless objects architecture students scan most.

---

## 3. System Layers

```
                       ┌─────────────────────────────────────────────────────┐
                       │  AGENT / ORCHESTRATION LAYER  (thin LLM shell)        │
                       │  scan_mcp.py  (stdlib MCP adapter, holds no state)    │
                       │      └─ forwards to → scan_service.py (HTTP, stateful)│
                       │  VLM judge via llm_backend.py (ollama|claude switch)  │
                       └───────────────┬─────────────────────────┬───────────┘
                                       │ tool calls               │ "VLM proposes"
                                       ▼                          ▼  geometry confirms
   ┌──────────────┐   ┌────────────────────────┐   ┌──────────────────────────┐
   │ TURNTABLE    │   │  CAPTURE LAYER          │   │  RECONSTRUCTION LAYER     │
   │ (deterministic│  │  TIER-1: watch-folder   │   │  Open3D tensor VBG (CUDA) │
   │  tool)        │  │   over CrealityScan      │   │  TSDF integrate → mesh    │
   │ rotate(deg)   │  │   exports (.obj/.ply/.stl)│  │  pose = turntable angle   │
   │ ESP32+TMC2209 │  │                          │   │  (synthesized) → ICP/    │
   │ serial / MQTT │  │  TIER-3: raw UVC depth   │   │   pose-graph polish      │
   └──────┬───────┘   │   via WSL2 (gated spike) │   └────────────┬─────────────┘
          │            └───────────┬─────────────┘                │
          │                        │                              │
          ▼                        ▼                              ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  DETERMINISTIC CORE  (NO LLM)  — postprocess.py                            │
   │  trimesh load/split → keep largest component (drop floaters/background)    │
   │  → fix normals/winding → fill small holes → pymeshfix watertight (print)   │
   │  → recenter / scale-check mm / orient Z-up → open3d quadric decimation     │
   │  → write <name>_clean.obj + sidecar.json (bbox_mm, tris, watertight, sid)  │
   │  → pyrender 6–8 offscreen thumbnails → catalog into Obsidian vault         │
   └──────────────────────────────────────────────────────────────────────────┘
          │                                                          │
          ▼                                                          ▼
   ┌──────────────┐                                          ┌──────────────┐
   │ RHINO MCP    │  pulls <name>_clean.obj + sidecar.json   │ BAMBU MQTT   │
   │ (downstream  │  (Z-up, mm, watertight)                  │ printability │
   │  consumer)   │                                          │ check + gated│
   └──────────────┘                                          │ print-start  │
                                                             └──────────────┘
```

### 3.1 Capture Layer (two tracks — see §6)
- **Tier-1 (shippable now):** a `watchdog` daemon over the CrealityScan export directory. Every manual export triggers the deterministic core. Zero dependence on raw capture or on solving the depth stream. Useful on day one.
- **Tier-3 (gated):** raw 16-bit depth + 8-bit IR + 8-bit RGB read directly from the per-function `/dev/videoN` nodes in WSL2 (custom UVC kernel + `usbipd-win` attach). This is the agentic capture loop, and it only exists if the Phase 0 spike succeeds.

### 3.2 Reconstruction Layer
- **Open3D tensor API (`o3d.t`) `VoxelBlockGrid`** as the integration core, `device=o3d.core.Device('CUDA:0')` on the RTX 4070 Ti SUPER. Legacy `ScalableTSDFVolume` as a CPU fallback/reference.
- Contract to integrate a frame: per-frame `uint16` depth + `uint8` RGB, **one** depth intrinsic `(fx,fy,cx,cy)`, **one** `depth_scale`, and a per-frame 4x4 extrinsic. Everything else (color extrinsic, loop closure, smoothing, Poisson) is cleanup.
- Tabletop voxel size 1.5–4 mm (start at 3 mm, refine to 1.5 mm once poses are good). `depth_max` clamped just past the turntable to reject background.

### 3.3 Pose-tracking Layer
- **Primary: turntable with a known per-step angle.** This collapses the pose problem from "estimate an N-pose 6-DOF trajectory and hope it doesn't drift" to "estimate ONE fixed screw axis once, then apply `R(axis, θ_i)` analytically." The 360° loop is consistent by construction — no accumulated drift, loop closure is free.
- **Axis self-calibration from the data, no chessboard:** scan a feature-rich object, pairwise point-to-plane ICP between adjacent frames, then SVD over the stacked `(R−I)` constraints recovers the common axis direction; a least-squares `(R_i−I)c = −t_i` recovers a point on the axis. Cache the axis as a rig property and reuse it for featureless objects.
- **Polish:** seed an Open3D PoseGraph with the synthesized step transforms, add odometry edges `i→i+1` AND the explicit loop edge `N−1→0`, run `global_optimization`. The known stepper angle is an ICP *initialization*, never ground truth (absorbs backlash/microstep error).
- **Handheld RGBD+IMU odometry is the fragile fallback only** for objects a turntable can't hold. It fails precisely on white/matte/glossy/flat surfaces (no stable geometric OR photometric features), and IMU only bridges short dropouts — it cannot fix scale or rotational drift. **Not the MVP.** Note: a rotating object in front of a fixed camera is the *inverse* of Open3D's static-scene assumption, so for the turntable we MUST segment/crop to the object and feed the known angle as a prior rather than trusting generic visual odometry (which would lock onto the static background).

### 3.4 Agent / Orchestration Layer
Mirror Zohar's existing `tools-dashboard` pattern verbatim — do not invent a framework:
- `scan_service.py` — stateful local HTTP service that owns sessions, talks to hardware + meshing (modeled on `serve.py`).
- `scan_mcp.py` — stdlib-only thin MCP adapter (urllib/json), holds no state, forwards every tool call to the token-guarded local HTTP API (modeled on `mcp_server.py`, `X-Tools-Dashboard-Token`, `127.0.0.1`, protocol `2024-11-05`).
- Copy `llm_backend.py` verbatim for the VLM judge so the `claude|ollama` toggle and prompts stay in one place.
- **VLM judge:** `qwen2.5vl:7b` on `gpu-pc` (fits 16GB at Q4/Q5 with KV headroom), flip to `claude` for final accept/reject. The VLM never sees geometry — it grades `pyrender` PNGs (6–8 canonical views, defects color-coded) and returns strict JSON `{has_holes, suspect_regions, coverage_estimate, verdict}`.
- **Hard rule: VLM proposes, deterministic geometry confirms, code executes.** The VLM never alone triggers a re-scan, a motor move, or a print. Open-boundary loops / manifold tests / local point density make the final call. Bambu MQTT print-start is gated behind explicit human confirmation. Re-scan loop is bounded (max N passes).

### 3.5 Turntable Layer
- **Passive MVP:** matte mid-grey platter (~200–240 mm, fits the 256 mm Bambu bed) on a 4-inch steel-ball lazy-susan bearing + leveling feet.
- **Markers:** OFFICIAL retroreflective stickers (6 mm typical, 3 mm small) placed in an **irregular** ring — NOT FDM-printed dots. Creality requires ≥4 shared markers between adjacent frames and irregular placement (a symmetric printed ring is rotationally ambiguous and FDM dots lack retroreflective contrast). The platter must be matte (CrealityScan's "Turntable=Yes" deletes the platter plane; pure white blooms IR, pure black returns no speckle).
- **Motorized v2:** NEMA17 + **TMC2209** (stealthChop + 1/256 interpolated microstepping = near-silent, vibration-free — vibration smears fusion) + ESP32 + ~20:1–40:1 reduction (PETG/metal worm, self-locking). Exposes `rotate(angle_deg)` over USB-serial (bench) and MQTT (reuse the Bambu broker). Indexed step-and-shoot: rotate → settle 300–500 ms → capture → repeat. The motor is invisible to CrealityScan — there is no Ferret auto-turntable integration; the agent orchestrates motor + scanner separately.

---

## 4. Current Platform Decision — supersedes the direct-USB investigation

Use **macOS + the running Orbbec RPCServer shipped with CrealityScan** for the
implemented capture path. The Python client connects to the daemon, discovers
the per-device method suffix, and requests frames without claiming USB itself.
Y14 disparity and Y16 computed-depth decoders live in ferret/capture.py.

The old macOS "IR-only" conclusion applied to direct AVFoundation/UVC access.
The former WSL2-only recommendation was made before the daemon RPC path was
established. WSL2/Linux direct-USB capture remains historical research, not a
required or verified host configuration for this SDK.

Live sensor delivery, daemon mode, stream switching and metric accuracy still
require hardware verification. The offline suite checks fake-RPC control flow
and deterministic decoding, and does not establish those live properties.
Mesh cleanup, NumPy reconstruction and native Open3D TSDF work offline. Native
TSDF uses `UniformTSDFVolume`, verified on synthetic plane and turntable-box depth
(FER-10); real multi-view fusion quality remains unverified. See
[logs/AUDIT-2026-11-15.md](logs/AUDIT-2026-11-15.md).

---

## 5. Intrinsics & Calibration Strategy

Two tiers, both standard:

- **Tier 1 (today, for a first mesh): FOV → pinhole approximation.**
  `fx = (w/2)/tan(hfov/2)`, `fy = (h/2)/tan(vfov/2)`, `cx = w/2`, `cy = h/2`.
  Wire via `intr.set_intrinsics(w,h,fx,fy,cx,cy)`. **Caveat:** Ferret FOV is not published — measure it or read from the device. Good enough to get geometry on screen.
- **Tier 2 (metric accuracy): one-time OpenCV `cv2.calibrateCamera`** on ~15–20 IR checkerboard captures. The IR stream is reachable even on macOS, and the printed checkerboard reads fine under ambient light despite the IR speckle. For an active-stereo cam the **depth map is rectified to the IR sensor, so the IR intrinsic IS the depth intrinsic** (verify this assumption empirically). For color, `cv2.stereoCalibrate` on simultaneous IR+RGB checkerboard pairs recovers the depth→RGB rigid transform.

- **`depth_scale` is a CALIBRATION DELIVERABLE, never a hardcoded constant.** There is no SDK to query it. Probe empirically: image a flat wall at tape-measured 200/300/400/500 mm, log mean raw `uint16` over a central ROI, and fit `raw → mm`. Test BOTH a linear `value→mm` and a disparity-style `mm ≈ a/raw + b` (active-stereo is often nonlinear). Persist the fit as a JSON the pipeline loads. A wrong scale silently produces a wrong-size or garbage mesh. Cross-check the resulting point-cloud scale against CrealityScan's output of the same object (you can A/B because both stacks share the machine).

- **Color registration:** ship a **depth-only (uncolored) mesh as v1** to de-risk. Add color only after confirming the device streams pre-registered color OR after `stereoCalibrate` recovers the extrinsic. Integrating color with the wrong depth→color transform smears texture.

---

## 6. The Two Tracks: Tier-1 (ship now) vs Tier-3 (gated)

This is the spine of the whole project. **Build Tier-1 first; it is valuable even if the depth spike fails entirely.**

### Tier-1 — Watch-folder post-processing daemon (LLM-optional, ships in an evening)
Independent of solving raw capture. On each new `OBJ/STL/PLY` exported by CrealityScan:
1. `trimesh.load`; keep largest connected component (drop floaters/background).
2. Record open-boundary stats **before** repair into the sidecar (so real coverage gaps aren't hidden).
3. `fix_normals` / `fix_winding`; fill small holes (threshold-limited); `pymeshfix` watertight pass for the "printable" copy (keep an un-repaired copy too).
4. Recenter to origin, scale-check (scanner outputs mm), orient longest-axis/PCA to Z-up.
5. `open3d`/`pymeshlab` quadric decimation to a target tri count for Rhino/Bambu.
6. Write `<name>_clean.obj` + `sidecar.json {bbox_mm, tris, watertight, session_id}` to a Rhino inbox.
7. `pyrender` 6–8 offscreen thumbnails.
8. Catalog into the Obsidian vault (`Creations/3D-Scans/`, append to `Daily/`) with a markdown quality report.
9. **Optional** toggle: run `qwen2.5vl:7b` over the thumbnails to add one human sentence ("hole under the handle; re-scan from below").

### Tier-3 — Raw-depth agentic capture (only if Phase 0 spike succeeds)
The full `scan_service.py` + `scan_mcp.py`: `start_session / capture / run_fusion / evaluate_quality / turntable_rotate / clean_mesh / decimate / export_mesh`. Closes the loop: rotate → capture → fuse → render → VLM grades coverage → targeted re-capture → post-process → export. **Inherits all of Tier-1's deterministic core** — Tier-3 is an additive layer, not a rewrite.

### The "Tier-2" middle option (computer-use automation of CrealityScan)
If the depth spike fails but Zohar still wants more automation than file-watching, a UI-automation layer could drive the CrealityScan GUI (open scenes, trigger fuse/export). Listed as an option, not recommended as a primary path — fragile and out of scope until Zohar asks.

---

## 7. Honest Risk Register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **Raw 16-bit depth is NOT a standard, directly-usable UVC stream.** Refuted (conf 0.72): zero community projects, needs firmware/init handshake, gated behind a vendor XU enable command. The depth function may never produce frames over a plain grab. | **HIGH — make-or-break** | Phase 0 spike validates this FIRST. If plain grab yields only IR, sniff CrealityScan's XU sequence (`usbmon` in WSL / USBPcap+Wireshark on Windows) and replay via `v4l2-ctl --set-ctrl` / libuvc `uvc_set_ctrl`. **Decouple completely: Tier-1 ships regardless.** If the spike fails, the project is still valuable via Tier-1 + the IR-based DIY depth route (R-alt). |
| R2 | **macOS cannot reach depth/RGB.** Confirmed (conf 0.9): `UVCAssistant` exclusive claim; escapes need root/entitlement; whole-device capture. | HIGH (already resolved) | Build on Windows/WSL2. Mac = thin client + IR-only checkerboard calibration. Do not invest in a macOS libusb depth path. |
| R3 | **`depth_scale` unknown / nonlinear.** A wrong scale silently yields a wrong-size or garbage mesh (TSDF band misaligned). | HIGH | Empirically calibrate against multiple tape-measured distances before any integration; assert center-pixel depth resolves to a plausible meter value; expose `depth_scale` as a tunable; model disparity nonlinearity; A/B against CrealityScan output. |
| R4 | **Pose drift on featureless/shiny/flat objects** (the objects architecture students scan most). Open3D's own docs + real user issues confirm fragment-integration drift and failure on symmetric/low-texture objects. | HIGH | Turntable with KNOWN per-step angle as the pose prior (not estimated odometry); segment/crop to the object so odometry doesn't lock onto the static background; matting spray + irregular retroreflective markers; bounded pose-graph polish. |
| R5 | **Custom WSL kernel is fiddly and breaks on WSL updates** (kernel mismatch, lost `.wslconfig kernel=` line, usbip module not built). | MEDIUM | Pin the kernel by version; keep the built `bzImage` + `.config` in the repo; document the exact tag. Fall back to a Hyper-V Ubuntu VM or native Linux which ship UVC/V4L2 out of the box. |
| R6 | **usbip isochronous USB3 video drops frames** under sustained streaming. | MEDIUM | Validate frame-drop rate before building higher layers; reduce res/fps; fall back to native Linux / VM with direct USB passthrough. |
| R7 | **Auto-repair hallucinates geometry over genuinely unscanned regions** — a falsely "watertight" but wrong mesh hides real coverage gaps. | MEDIUM | Detect holes BEFORE the watertight repair; record original open-boundary stats in the sidecar; only auto-fill holes below a size threshold; flag large filled regions in the vault report; keep an un-repaired copy. |
| R8 | **LLM/VLM in the control loop** introduces nondeterminism/latency/silent failures where geometry is correct; `qwen2.5vl:7b` may mislocate small holes. | MEDIUM | Hard rule: VLM proposes, geometry confirms, code executes. Color-code defects in renders; require strict JSON; validate every suspect region geometrically before acting; flip to Claude for final accept/reject; bound the re-scan loop. |
| R9 | **Auto-triggering a Bambu print or turntable move** on a bad mesh wastes filament / risks hardware. | MEDIUM | Never auto-execute physical/financial actions (global rule): gate MQTT print-start behind explicit human confirm; bound turntable angle limits; printability pre-check (bbox ≤ ~256 mm bed, watertight, wall-thickness heuristic). |
| R10 | **Printed regular dot ring / wrong platter finish** breaks marker tracking (rotational ambiguity, no contrast) or pollutes the scan (specular IR / black no-return). | MEDIUM | Official irregular retroreflective stickers, ≥4 visible per frame; matte mid-grey platter; test a swatch under the IR stream first. |
| R11 | **Object slips / isn't rigid on the platter**, breaking the single-axis assumption. | LOW–MED | Fixture the object; do a fast full rotation to confirm tracking before the real capture; global ICP absorbs small deviations; a large residual flags a slip to re-shoot. |
| R-alt | **If R1 kills raw depth entirely:** the IR stream IS confirmed-capturable. | (fallback) | Grab 1280x800 IR speckle frames over plain UVC and run YOUR OWN active-stereo/structured-light depth on the host (the GPU is there), bypassing the locked depth function. Higher effort, but a real escape hatch. |

---

## 8. Open Questions (resolve early; most are settled by Phase 0)

1. **Does the depth UVC function advertise a 16-bit uncompressed format** (Y16/Z16) in its frame descriptors, or a vendor-encoded payload needing custom unpacking? → `v4l2-ctl --list-formats-ext` per node once attached to WSL2. (Phase 0)
2. **What exact vendor XU unit-id + selector + payload does CrealityScan send to enable the depth engine / set IR laser power?** → `usbmon`/USBPcap capture while CrealityScan runs. (Phase 0.5)
3. **Is the depth map rectified to the IR sensor** (so IR intrinsic == depth intrinsic), and is depth pre-registered to RGB? → probe + checkerboard.
4. **Is the IMU HID-class** (auto-populates Linux IIO) **or vendor-class 0xFF** (raw pyusb interrupt reads)? → `lsusb -v` on the IMU interface. (Low priority — defer; turntable removes the need for IMU pose.)
5. **Does usbip sustain USB3 isochronous depth+IR+RGB bandwidth** without frame drops, or is native Linux/VM needed for production rate?
6. **Can CrealityScan's turntable mode export raw per-frame clouds**, or only a fused mesh? If only fused, the DIY capture path must bypass it entirely.
7. **Turntable v1 manual-index or motorized from the start?** Manual-index lets Tier-1 + a minimal Tier-3 ship far sooner.
8. **Is the cached rotation axis stable across power cycles / tripod re-setups**, or must axis self-calibration run at the start of every session?

---

## 9. Go / No-Go Posture

**GO**, with a staged commitment. Tier-1 (watch-folder post-processing + vault cataloging + optional VLM note) is **unconditionally worth building** — it delivers value on every scan and has zero dependence on the risky depth work. The risky, high-reward Tier-3 (raw agentic capture) is **conditional on the Phase 0 spike**. Build Tier-1 in parallel while Phase 0 runs, so a failed spike never produces a dead project — only a smaller one.
