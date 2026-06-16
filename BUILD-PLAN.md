# ferret-sdk — Phased Build Plan

Sequenced so the **riskiest assumption is validated first** (Phase 0: can we get a
raw 16-bit depth frame at all?) while the **always-valuable Tier-1 daemon proceeds
in parallel** (Track B). Each phase has ONE verifiable success criterion in the
Karpathy "make this assertion true" style. Keep code minimal; prefer existing deps.

**Two parallel tracks after Phase 0 kicks off:**
- **Track A (risky, high-reward):** raw depth → agentic capture (Phases 0 → 1 → 4 → 5 → 6).
- **Track B (safe, ships regardless):** watch-folder post-processing + vault (Phases B1 → B2), can start the same day as Phase 0 and never blocks on it.

Hardware turntable (Phases T1 → T2) slots in independently once any reconstruction path works.

---

## PHASE 0 — Make-or-break depth-capture spike (Windows + WSL2)  ⚠️ RISKIEST

**Goal:** Determine empirically whether the Ferret's 16-bit Depth UVC function can be
read at all without the Creality SDK. This single result decides whether Track A is
alive. Do this BEFORE writing any pipeline code on top of depth.

**Concrete tasks:**
1. (Admin PowerShell) `usbipd list` → find busid for VID `2BC5` PID `069C`; `usbipd bind --busid <b-d>` (once); `usbipd attach --wsl --busid <b-d>`. Confirm in WSL: `lsusb | grep -i 2bc5`.
2. Build a UVC-enabled WSL kernel: clone the matching `WSL2-Linux-Kernel` tag, merge PINTO0309's usbcam `.config` (`CONFIG_USB_VIDEO_CLASS=m`, `CONFIG_MEDIA_USB_SUPPORT=y`, `CONFIG_VIDEO_V4L2=m`, `CONFIG_USB_VIDEO_CLASS_INPUT_EVDEV=y`), `make -j16`, copy `bzImage` to `C:\Users\<you>\`, point `[wsl2] kernel=<path>` in `%UserProfile%\.wslconfig`, `wsl --shutdown`, reattach. **Commit the built `bzImage` + `.config` + exact kernel tag into the repo.**
3. Enumerate every function: `for d in /dev/video*; do echo "== $d =="; v4l2-ctl -d "$d" --list-formats-ext; done`. Map nodes by format: 1280x800 8-bit (UYVY/NV12) = IR; a node advertising `Y16 '/'Z16 '/16-bit = DEPTH; MJPG/YUYV node = RGB. (Ignore uvcvideo metadata nodes.)
4. Attempt a plain raw grab on the depth node WITHOUT any SDK:
   `v4l2-ctl --device=/dev/videoX --stream-mmap --stream-count=1 --stream-to=depth.raw`
5. Write a ~20-line `probe/depth_to_png.py`: read `depth.raw` as `np.frombuffer(..., np.uint16).reshape(H, W)`, save a 16-bit PNG, print min/max/mean. Eyeball whether values vary plausibly with object distance.
6. If the depth node refuses to stream / emits constant/garbage → it's gated behind a vendor XU. Capture the enable sequence: `sudo modprobe usbmon` + Wireshark (or USBPcap on Windows) while CrealityScan starts a scan natively. Save the `.pcapng` to `probe/`.

**✅ VERIFIABLE SUCCESS CRITERION:** `probe/depth_to_png.py` saves a **non-empty 16-bit PNG from the depth `/dev/videoN` node whose center-pixel value changes monotonically when an object is moved closer/farther** (proving it's real depth, not a static pattern). — If only a constant/garbage frame or IR-like speckle comes out, the criterion FAILS and Track A pivots to Phase 0.5.

**Dependencies:** Windows desktop, `usbipd-win` (installed), WSL2, `v4l-utils`, `python3-numpy`. The physical scanner.

**Decision gate:** PASS → Phase 1. FAIL → Phase 0.5 (XU recovery). FAIL-after-0.5 → Track A dies; fall back to Track B + IR-DIY (Phase 0-alt). Track B continues regardless.

---

## PHASE 0.5 — XU enable-sequence recovery (only if Phase 0's plain grab failed)

**Goal:** Recover and replay the vendor UVC Extension Unit command that turns the depth
engine on, so a third-party tool can start the stream.

**Concrete tasks:**
1. From the `probe/*.pcapng`, isolate the UVC class control transfers (`SET_CUR` to the XU unit) CrealityScan sends at scan start. Note unit-id, selector, payload bytes.
2. Map the XU on Linux via `UVCIOC_CTRL_QUERY` / a `v4l2-ctl` extension-unit control, or libuvc `uvc_set_ctrl(devh, unit_id, selector, payload, len)`.
3. Replay the sequence, then re-run the Phase 0 raw grab.

**✅ VERIFIABLE SUCCESS CRITERION:** after replaying the captured XU `SET_CUR`, the same `probe/depth_to_png.py` grab now produces a depth-varying 16-bit PNG (Phase 0's criterion, now passing).

**Dependencies:** Phase 0 pcap capture; `v4l-utils` or `libuvc`.

**Decision gate:** PASS → Phase 1. FAIL → Track A dead; Phase 0-alt (IR-DIY depth) or Track-B-only.

---

## PHASE 0-alt — IR-based DIY depth (escape hatch, only if 0 + 0.5 both fail)

**Goal:** Bypass the locked depth function entirely by computing depth from the
confirmed-capturable IR speckle stream.

**Concrete tasks:** capture 1280x800 IR frames over plain UVC (already proven in `probe/grab_ir.sh`); run a structured-light/active-stereo depth estimator on the host GPU. This is a research effort, scoped only if it becomes the sole path.

**✅ VERIFIABLE SUCCESS CRITERION:** a host-side depth estimator produces a depth map from a single IR frame whose values track a tape-measured flat target across 200/300/400 mm.

**Dependencies:** IR capture (done); GPU. **High effort — last resort.**

---

## PHASE 1 — Stable multi-stream capture + depth calibration (Track A, after Phase 0/0.5 PASS)

**Goal:** A reliable Python capture service that reads depth + IR + (optionally RGB)
at rate, and a persisted empirical `depth_scale` calibration.

**Concrete tasks:**
1. `capture.py`: raw 16-bit depth via `linuxpy`/`v4l2py` (`np.frombuffer(buf, np.uint16).reshape(H,W)` — OpenCV mangles 16-bit). IR/RGB 8-bit via `cv2.VideoCapture(N, cv2.CAP_V4L2)` + `cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)`. Bind by device path, not unstable integer index.
2. Measure frame-drop rate over 60 s sustained streaming (validates R6 before building higher layers).
3. `calibrate_depth.py`: image a flat wall at tape-measured 200/300/400/500 mm; log mean raw `uint16` over a central ROI; fit BOTH linear and `a/raw + b`; pick the better fit; write `calib/depth_scale.json`.
4. Tier-2 intrinsics: `cv2.calibrateCamera` on ~15–20 IR checkerboard shots → `calib/ir_intrinsics.json` (= depth intrinsic for an active-stereo cam).

**✅ VERIFIABLE SUCCESS CRITERION:** `capture.py` runs for 60 s saving paired depth(`uint16`)+IR frames with **<2% dropped frames**, AND `calib/depth_scale.json` maps a raw center-pixel value at a tape-measured 300 mm to **300 ± 10 mm**.

**Dependencies:** Phase 0/0.5; `v4l-utils`, `linuxpy` (or `v4l2py`), `opencv-python`, `numpy`. Printed checkerboard, tape measure, flat wall.

---

## PHASE B1 — Tier-1 deterministic mesh core (Track B, START SAME DAY AS PHASE 0)

**Goal:** A pure function that turns any CrealityScan-exported mesh into a clean,
Z-up, mm-scaled, Rhino-ready file + sidecar JSON + thumbnails. **Zero dependence on
raw capture — useful even if all of Track A fails.**

**Concrete tasks:**
1. `postprocess.py` taking an input path:
   - `m = trimesh.load(path)`; `m = max(m.split(only_watertight=False), key=lambda c: c.area)` (drop floaters/background).
   - Record open-boundary stats **before** repair → sidecar.
   - `trimesh.repair.fix_normals(m)`, `fix_winding(m)`, `fill_holes(m)` (size-thresholded); `pymeshfix` watertight pass for the printable copy; keep an un-repaired copy.
   - Recenter to origin; scale-check mm; PCA/longest-axis to Z-up.
   - `open3d`/`pymeshlab` quadric decimation to a target tri count.
   - Write `<name>_clean.obj` + `sidecar.json {bbox_mm, tris, watertight, open_boundaries_before, session_id}`.
2. `render_views.py`: `pyrender.OffscreenRenderer` (EGL on the 4070 Ti, OSMesa fallback) → 6–8 canonical-view PNGs, defects (boundary edges / curvature) color-coded.

**✅ VERIFIABLE SUCCESS CRITERION:** `python postprocess.py sample.obj` on a CrealityScan export produces a `<name>_clean.obj` with exactly **one connected component, watertight==True (printable copy), bbox in mm**, plus 6 non-empty thumbnail PNGs.

**Dependencies:** `trimesh`, `pymeshfix`, `open3d` or `pymeshlab`, `pyrender`, `numpy`. One sample CrealityScan export (hand-export one to bootstrap).

---

## PHASE B2 — Watch-folder daemon + vault cataloging + optional VLM note (Track B)

**Goal:** The shippable Tier-1 product: every manual CrealityScan export is
auto-processed and cataloged into the Obsidian vault.

**Concrete tasks:**
1. `watch_daemon.py`: `watchdog` on the CrealityScan export dir; on each new `OBJ/STL/PLY` call `postprocess.py` + `render_views.py`.
2. Catalog into the vault: copy output to `Creations/3D-Scans/`, append a session line to `Daily/YYYY-MM-DD.md`, write a markdown quality report (tri count, watertight, bbox mm, open-boundary count, filled-region flag).
3. **Optional** (config flag, default OFF): copy `llm_backend.py`; run `qwen2.5vl:7b` over the thumbnails for a one-sentence defect note appended to the report. Health-check `desktop-zt:11434` first; fail fast with the Claude fallback if the PC is off.

**✅ VERIFIABLE SUCCESS CRITERION:** dropping a fresh `.obj` into the watched folder produces, within 30 s and with no manual step, a `_clean.obj` + thumbnails in `Creations/3D-Scans/` AND a new dated entry in the vault `Daily/` log.

**Dependencies:** Phase B1; `watchdog`; vault path; (optional) `llm_backend.py` + Ollama on `desktop-zt`.

---

## PHASE 2 — First reconstructed mesh from raw depth (Track A; needs Phase 1)

**Goal:** Prove the Open3D TSDF path end-to-end with the simplest possible poses.

**Concrete tasks:**
1. `reconstruct.py`: Open3D tensor `VoxelBlockGrid` (`attr_names=('tsdf','weight','color')`, `voxel_size=0.003`, `block_resolution=16`, `block_count=50000`, `device=CUDA:0`).
2. Capture a SHORT sequence of an object on a manually-indexed turntable at known angles (e.g. 12 steps × 30°).
3. Synthesize extrinsics as rotations about a hand-measured vertical axis (refine later); per frame: `compute_unique_block_coordinates` → `integrate(...)` with `depth_scale` from `calib/depth_scale.json`, `depth_max≈0.8`.
4. `mesh = vbg.extract_triangle_mesh().to_legacy()`; feed through Phase B1's `postprocess.py`.

**✅ VERIFIABLE SUCCESS CRITERION:** `reconstruct.py` on a 12-frame turntable sequence outputs a single-component `.obj` whose **bounding-box dimensions match the real object's caliper-measured dimensions within ±5 mm**.

**Dependencies:** Phase 1 (depth + scale + intrinsics); Phase B1 (postprocess); `open3d` CUDA build; a manually-indexed turntable (Phase T1 helps but a Lazy Susan + protractor works).

---

## PHASE T1 — Passive turntable (hardware; parallel, independent)

**Goal:** A physical capture aid that makes registration deterministic.

**Concrete tasks:** print a ~200–240 mm matte mid-grey platter; 4-inch steel-ball lazy-susan bearing; 3–4 leveling feet; irregular ring of OFFICIAL retroreflective stickers (≥4 visible/frame); matting spray on hand for shiny objects.

**✅ VERIFIABLE SUCCESS CRITERION:** a test object on the platter completes a full 360° rotation in CrealityScan with tracking staying "green" (no marker-ambiguity / lost-track failure) for the whole revolution.

**Dependencies:** Bambu printer, bearing, official markers. None on software.

---

## PHASE 3 — Axis self-calibration + pose-graph polish (Track A; needs Phase 2)

**Goal:** Replace the hand-measured axis with a calibrated one and drift-correct the loop.

**Concrete tasks:**
1. `calibrate_axis.py`: scan a feature-rich object; pairwise point-to-plane ICP between adjacent frames → `R_i`; SVD over stacked `(R−I)` for axis direction; least-squares `(R_i−I)c=−t_i` for a point on the axis; cache `calib/turntable_axis.json`.
2. `step_transform(axis, center, θ)` via Rodrigues; seed an Open3D `PoseGraph` (odometry edges `i→i+1` + explicit loop edge `N−1→0`); `global_optimization(... LevenbergMarquardt ...)`; re-integrate all frames at optimized poses.

**✅ VERIFIABLE SUCCESS CRITERION:** the calibrated-axis + pose-graph reconstruction reduces the visible double-wall/ghosting on a full 360° scan versus Phase 2's hand-axis result, AND the loop-closure residual (frame N−1 → 0) is **< 1 voxel (3 mm)**.

**Dependencies:** Phase 2; `open3d`, `numpy`/`scipy`.

---

## PHASE T2 — Motorized turntable + agent control (hardware + firmware; parallel)

**Goal:** Known, repeatable step angles driven by the agent.

**Concrete tasks:** NEMA17 + TMC2209 (UART) + ESP32 + ~20:1–40:1 reduction (PETG/metal worm); firmware exposes USB-serial (`R 15\n`, `HOME`, `STATE?`) AND MQTT (`turntable/cmd`, `turntable/state`) reusing the Bambu broker. Step math: `steps_per_output_deg = (steps_per_rev_motor * microsteps * reduction)/360`.

**✅ VERIFIABLE SUCCESS CRITERION:** publishing `{"action":"rotate","deg":15}` to `turntable/cmd` rotates the platter 15° (protractor-verified ±1°) and the ESP32 publishes `turntable/state: settled` within 2 s.

**Dependencies:** Phase T1 platter; NEMA17, TMC2209, ESP32, reduction, 12V PSU; Bambu MQTT broker.

---

## PHASE 4 — scan-service + MCP tool surface (Track A; needs Phase 3, mirrors tools-dashboard)

**Goal:** Expose the pipeline to the agent via the existing MCP pattern.

**Concrete tasks:**
1. `scan_service.py`: stateful local HTTP service (token-guarded, `127.0.0.1`) owning sessions + hardware + meshing; reuses Phase 1–3 + B1.
2. `scan_mcp.py`: stdlib thin MCP adapter (modeled on `mcp_server.py`) forwarding tool calls. Tools: `start_session`, `capture`, `run_fusion`, `evaluate_quality`, `turntable_rotate`, `clean_mesh`, `decimate`, `export_mesh` (writes `<name>_clean.obj` + sidecar — the shared contract for Rhino + Bambu).
3. `evaluate_quality` returns deterministic metrics (`coverage_pct`, `open_boundary_loops`, `watertight`) + optional VLM verdict.

**✅ VERIFIABLE SUCCESS CRITERION:** an MCP client calls `start_session` → `turntable_rotate(15)` → `capture` → `run_fusion` → `export_mesh` and receives a path to a valid `_clean.obj` + sidecar, with `scan_service.py` holding the session state.

**Dependencies:** Phases 3, T2, B1; copy of `mcp_server.py` + `llm_backend.py` patterns.

---

## PHASE 5 — Closed-loop VLM-judged coverage (Track A; needs Phase 4)

**Goal:** The agent decides WHERE coverage is missing and targets re-captures —
under the "VLM proposes, geometry confirms, code executes" rule.

**Concrete tasks:**
1. `render_views.py` → `qwen2.5vl:7b` with a strict-JSON prompt `{has_holes, suspect_regions:[{view,bbox,reason}], coverage_estimate, verdict}`.
2. For each suspect region, deterministic geometry confirms (open boundary loops / local point density) BEFORE acting; orchestrator commands `turntable_rotate` to that angle + re-captures. Bound to max N passes; then "flag for manual re-scan."

**✅ VERIFIABLE SUCCESS CRITERION:** on an object scanned with a deliberate gap, the loop detects the hole (VLM proposes + geometry confirms an open-boundary loop at that region), issues a targeted `turntable_rotate` + re-capture, and the re-integrated mesh has **fewer open-boundary loops** than before — without any unconfirmed VLM-only action.

**Dependencies:** Phase 4; Ollama `qwen2.5vl:7b` on `desktop-zt`; Claude fallback via `llm_backend.py`.

---

## PHASE 6 — Rhino + Bambu downstream (decoupled consumers; needs B1 or Phase 4)

**Goal:** Wire the exported mesh into Zohar's existing Rhino MCP and Bambu pipeline,
keeping them decoupled from the capture loop.

**Concrete tasks:**
1. Rhino MCP imports `<name>_clean.obj` + sidecar from the inbox on demand (Z-up, mm, watertight). No synchronous push from scan-service.
2. Bambu printability check: deterministic `bbox ≤ ~256 mm bed` + watertight + min-wall heuristic; optional MQTT print-start **gated behind explicit human confirm**.

**✅ VERIFIABLE SUCCESS CRITERION:** a `_clean.obj` imports into Rhino correctly oriented and scaled in mm, AND the printability check returns a correct pass/fail for both an in-bed and an over-bed test mesh.

**Dependencies:** Phase B1 or 4; Rhino MCP (`grasshopper-mcp` skill); Bambu MQTT stack.

---

## Sequencing summary

```
Day 0:  PHASE 0 (depth spike)  ───────────────────────┐   PHASE B1 (mesh core) ──┐
                                                        │                          │
        PASS↓        FAIL→0.5→FAIL→0-alt/Track-B-only   │                          ▼
        PHASE 1 (capture+calib)                         │                  PHASE B2 (watch+vault)  ◄── ships value here
        PHASE 2 (first mesh)  ◄── needs B1 postprocess ─┘                          │
        PHASE 3 (axis+pose-graph)                                                  │
        PHASE 4 (scan-service+MCP)                                                 │
        PHASE 5 (VLM coverage loop)                                                │
        PHASE 6 (Rhino+Bambu)  ◄───────────────────────────────────────────────── ┘

Parallel hardware:  PHASE T1 (passive)  →  PHASE T2 (motorized)
```

**The one rule:** never let Track A's risk block Track B's value. Phase 0 and Phase B1
start the same day. If Phase 0 fails for good, Zohar still has the Tier-1 watch-folder
daemon (B1+B2) + the downstream Rhino/Bambu wiring (Phase 6) — a genuinely useful tool.
