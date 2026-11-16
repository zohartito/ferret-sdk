# Ferret SDK — Historical Completion Report

> Historical private-fixture session. Current public-clone validation is in
> [logs/AUDIT-2026-11-15.md](logs/AUDIT-2026-11-15.md). The original 47/47
> count and real-frame component counts below are not current results; the
> 2026-11-15 baseline was 64 passed, 7 skipped. Native Open3D TSDF now uses
> `UniformTSDFVolume`, verified offline on synthetic plane and turntable-box
> depth (FER-10); real multi-view fusion quality remains unverified.

Built the full Ferret 3D-scanning SDK + pipeline **offline**. The live scanner and
its daemon were **never touched** (no socket to `127.0.0.1:10010`, no CrealityScan,
no USB) — capture code is refactored from the proven probe and verified via parsing
logic + decode round-trips, not by connecting to hardware.

**Historical test status:** `47/47 pytest tests pass` (`.venv/bin/python -m pytest -q`), in ~2.4 s.

---

## What was built (per component)

| Component | Files | Tests | Key artifact |
|---|---|---|---|
| **calib** | `ferret/calib.py` | 5/5 (`tests/test_calib.py`) | `artifacts/real_depth_pointcloud.ply` |
| **capture** | `ferret/capture.py`, `ferret/rpc_client.py` | 9/9 (`tests/test_capture.py`) | `artifacts/capture_y16_depth_mm.npy`, `..._preview.png`, `capture_y14_depth_mm_synth.npy` |
| **reconstruct** | `ferret/reconstruct.py` | 7/7 (`tests/test_reconstruct.py`) | `artifacts/real_recon.ply`, `synthetic_recon.obj` |
| **meshpipe** | `ferret/meshpipe.py` | 4/4 (`tests/test_meshpipe.py`) | `artifacts/messy_input.obj`, `cleaned_output.obj` |
| **mcp** | `ferret_mcp.py` | 7/7 (`tests/test_mcp.py`) | `artifacts/test_mcp_scan.ply` |
| **turntable-cad** | `hardware/turntable/turntable.scad`, `BOM.md` | render check | `artifacts/turntable.stl`, `turntable.png` |
| **turntable-fw** | `hardware/turntable/turntable_control.py`, `firmware/turntable.ino`, `README.md` | 14/14 (`tests/test_turntable.py`) | — |

All modules compose: `import ferret` brings up `calib`, `capture`, `reconstruct`,
`meshpipe`; `reconstruct` consumes `calib.Intrinsics` + `calib.depth_to_points`;
`ferret_mcp` calls `reconstruct` and `meshpipe`.

---

## Historical offline measurements (private inputs required)

Ground truth: `probe/rpc_depth.npy` — a REAL metric-depth frame (uint16, 800×640,
`depth_mm = value × 0.2`, 0/65535 invalid).

**calib**
- `intrinsics(640, 800)` → `fx = fy = 478.3355 px` (= `956.671 × 640/1280`), `cx = 320.0`, `cy = 400.0`. `intrinsics(1280, 800).fx == 956.671` exactly; fx==fy and scale linearly with width.
- `depth_to_points` on the real frame → **183 412 points** (= valid pixels after the 0.05–8 m range filter; 184 126 raw non-sentinel pixels, 714 near/far outliers dropped). Output float32 Nx3.
- Metric bounds physically sane: Z min/median/max = **0.0512 / 1.0280 / 7.1170 m**; X ∈ [−0.774, 3.276] m, Y ∈ [−3.073, 2.143] m, all finite.

**capture**
- Y16 decode of the real raw blob (`probe/rpc_raw_frame.bin`, 1 024 000 bytes) reproduces `rpc_depth.npy × 0.2` **byte-exact** (array-equal); 184 126 valid pixels.
- Y14 pack/unpack are exact inverses on a known 4-px group and on a full random 1280×800 14-bit frame (packed length = 1 792 000 bytes); little-endian 4px/7byte bit layout asserted byte-exact.
- Disparity→depth self-consistent: uniform `raw ≈ 872` → **657.34 mm = z0** to rel 1e-4; synthetic close-range frame round-trips with mean quantization error 0.046 mm.
- Decode dispatch routes 1 024 000-byte blobs → Y16, 1 792 000-byte → Y14.

**reconstruct**
- Real frame → 183 412-pt cloud written to `artifacts/real_recon.ply` (2.2 MB), reloads with matching count.
- Synthetic turntable: 80×60×50 mm box rendered from 12 angles, fused via inverse-rotation poses → measured bbox **[81.15, 61.27, 51.03] mm** vs expected [80, 60, 50] (rel err 1.4% / 2.1% / 2.1%, within 10%). `synthetic_recon.obj` watertight (152 verts, 300 faces).
- **FER-10 (2026-11-15):** Open3D 0.20.0's `ScalableTSDFVolume` integrates no
  voxels on macOS arm64 (Open3D's own PrimeSense example included), so `fuse()`
  silently used the NumPy fallback. Native fusion now uses a `UniformTSDFVolume`
  sized to the posed depth points (at most 256 voxels per side), and
  `mesh.metadata["fusion_backend"]` records which path ran. The strict test fuses
  dense 600 mm plane depth natively (surface within one voxel) and the 12-view
  turntable box; a second test checks the fallback is labelled. The historical
  real-frame claim is not reproducible without its private input.

**meshpipe**
- `clean_mesh` on a messy synthetic mesh: faces **81 985 → 50 000** (exact decimation target), components **106 → 1**, `is_watertight=True`, `euler_number=2`.
- Recenter: cleaned bbox centroid = [0,0,0] (atol 1e-6). Z-up: asymmetric X-long capsule reoriented so the long axis maps to Z with no distortion. Hole-fill: open sphere → watertight (euler 2).
- `watch_folder` bounded cycle: 2 supported meshes cleaned, non-mesh file skipped.
- Made networkx-free (scipy connected-components + PyMeshLab hole fill), so the pipeline runs with only numpy+scipy+trimesh+open3d+pymeshlab present.

**mcp**
- All 5 tools registered on FastMCP server `ferret`: `depth_stats`, `reconstruct_pointcloud`, `clean_mesh`, `turntable_rotate`, `list_artifacts`.
- `depth_stats` on the real frame: shape [800, 640], 184 126 valid (35.96%), p25/p50/p75 = 514.4 / 1028.0 / 1388.4 mm.
- `reconstruct_pointcloud` on the real frame → `artifacts/test_mcp_scan.ply` (183 412 points).
- `clean_mesh` **verified working** (50 000 faces, watertight) — the networkx blocker noted earlier is resolved by meshpipe's scipy/pymeshlab routing.

**turntable**
- CAD: `openscad` renders `turntable.scad` → STL, CGAL reports manifold; trimesh confirms 4796 tris, watertight, bbox **200.00 × 200.00 × 12.00 mm** (8 mm platter + 4 mm ledge), 100 mm bearing recess.
- Firmware/host: `TurntableController.rotate(90)` writes exactly `b'R90\n'`; `-45.5`→`b'R-45.5\n'`; `set_speed`/`home`/step-sequence verified; 360° wraparound tracked; ERR/empty/unexpected replies raise. Firmware step math: 3200 steps/rev (200×16 µstep), R90 = exactly 800 steps. 14/14 tests vs a fake serial transport.

---

## Adversarial-review fixes applied this session

The adversarial pass found 5 issues. Fixed the HIGH + both MEDIUMs (small, safe) and
the LOW + INFO doc issues; re-ran the affected suites (still 47/47).

1. **HIGH — color/point validity-mask desync** (`reconstruct.depth_to_pointcloud`). The color sampler recomputed a looser `depth_mm > 0` mask while points used calib's full range filter, so colors silently misaligned whenever near/far outliers existed (and they do: 714 in the real frame). **Fix:** `calib.depth_to_points` now takes `return_mask=True` and returns the exact kept-pixel boolean mask; `reconstruct._project` threads it through and samples colors at that same mask. Added a `calib.valid_mask()` as the single source of truth. Regression test `test_colors_stay_aligned_with_points_when_outliers_dropped` (4×4 frame: a below-floor + an above-ceiling pixel) now asserts points==colors==1 with the correct color.
2. **MEDIUM — impossible `_Y16_SHAPE_TABLE` entries** (`capture.py`). `480_000:(800,600)` and `256_000:(400,640)` are arithmetically impossible (Y16 is 2 B/px) and raised `ValueError` on reshape, defeating the safe fallback. **Fix:** removed both unverified entries; only the real-data-verified `1_024_000:(800,640)` remains; odd sizes fall through to the 640-wide heuristic without crashing (confirmed 480 000 → (375,640), 256 000 → (200,640), no raise).
3. **MEDIUM — missing Y14 high sentinel** (`capture.disparity_to_depth_mm`). `raw=0x3FFF` (14-bit all-ones, "no return") leaked a bogus ~18 mm near-point. **Fix:** added `Y14_INVALID=0x3FFF` and a `MIN_VALID_DEPTH_MM=50.0` floor; sentinel and absurdly-near survivors now map to 0.0. Regression test `test_disparity_rejects_y14_high_sentinel`.
4. **LOW — dead sentinel guard in calib.** The `65535` check ran in the mm domain (where 65535 raw arrives as 13107 mm, never matching). **Fix:** `valid_mask` checks only the 0 sentinel explicitly and relies on the 8 m range bound for the high sentinel; clarified the comment on `INVALID_DEPTH_VALUES`.
5. **INFO — misleading z0/disp_offset docstring** (`capture.disparity_to_depth_mm`). Reworded to state the relationship is a numerical coincidence of the logged constants (`baseline·focal/z0 ≈ 217.9994 ≈ disp_offset`, so `raw ≈ 872` maps to z0), not an identity.

Files touched: `ferret/calib.py`, `ferret/reconstruct.py`, `ferret/capture.py`,
`tests/test_reconstruct.py`, `tests/test_capture.py`. `artifacts/real_recon.ply`
regenerated through the fixed path (183 412 pts, z 0.0512–7.117 m, reloads clean).

---

## What did NOT work / could not be verified (needs the live device)

These are honest gaps — none are bugs, all require hardware we deliberately did not touch.

- **Live capture path.** `FerretCamera.open()/get_depth_mm()/get_ir()/close()` and the
  msgpack-RPC sequence (`start_stream`/`get_frame`/`stop_stream`) are refactored verbatim
  from the proven probe but were **not** run against the daemon (hard safety rule). Untested live.
- **Suffix auto-discovery against a live server.** `_candidate_suffixes` parse logic is
  unit-covered, but the `ob_get_stream_profile_list<suffix>` RPC round-trip was not exercised.
- **Y14 delivery from the daemon.** The unpack+disparity math is verified on synthetic
  packed bytes, but whether the daemon actually emits the 1 792 000-byte Y14 frame (vs
  computed Y16) depends on the GUI's scan-start `send_data` setup — needs one live raw capture.
- **`get_ir()` decode on real data.** Coded from PROTOCOL.md; no saved Y8 byte blob exists
  to validate the reshape.
- **Absolute metric accuracy vs a measured target.** Calibration constants are taken from
  the device log as given; not validated against a physical object of known size.
- **Multi-view fusion on real geometry.** Only one real depth frame exists offline, so real
  multi-pose turntable fusion and dense-capture TSDF quality are unproven (validated only on
  the single real frame + synthetic box).
- **Turntable firmware on hardware.** `.ino` not compiled/flashed; real stepper accuracy,
  Vref tuning, thermal behavior, MQTT-over-WiFi, and end-to-end USB-serial round-trip are
  untested (host tested only against a fake transport).
- **Physical fits (turntable CAD).** 100 mm lazy-susan bearing recess, marker-ring seat, and
  matte-top print quality not test-fitted; recess has no added clearance (assume +0.2–0.4 mm
  after a print).
- **Live MCP stdio loop.** `mcp.run` verified callable but not started (no MCP client; not the device).

---

## Artifacts to inspect (absolute paths)

- `/path/to/ferret-sdk/artifacts/real_recon.ply` — real frame → 183 412-pt cloud (regenerated post-fix)
- `/path/to/ferret-sdk/artifacts/real_depth_pointcloud.ply` — calib's real-frame cloud
- `/path/to/ferret-sdk/artifacts/synthetic_recon.obj` — fused synthetic box (watertight)
- `/path/to/ferret-sdk/artifacts/cleaned_output.obj` — meshpipe output (50 000 faces, 1 component, watertight)
- `/path/to/ferret-sdk/artifacts/messy_input.obj` — its messy input (106 components)
- `/path/to/ferret-sdk/artifacts/test_mcp_scan.ply` — MCP `reconstruct_pointcloud` output
- `/path/to/ferret-sdk/artifacts/capture_y16_depth_preview.png` — Y16 depth preview
- `/path/to/ferret-sdk/artifacts/capture_y16_depth_mm.npy`, `capture_y14_depth_mm_synth.npy` — decoded depth arrays
- `/path/to/ferret-sdk/artifacts/turntable.stl`, `turntable.png` — 200×200×12 mm platter + preview

Inspect a PLY/OBJ quickly:
```bash
.venv/bin/python -c "import trimesh; m=trimesh.load('artifacts/cleaned_output.obj'); print(m, m.is_watertight)"
```

---

## Where to give feedback

Review these decisions/files — they encode judgment calls worth a second opinion:

1. **`ferret/calib.py` — validity band & principal point.** `MIN_VALID_DEPTH_M=0.05`,
   `MAX_VALID_DEPTH_M=8.0`, and `cx=W/2, cy=H/2`. The range drops 714 real pixels; is 8 m
   the right ceiling for your scan subjects, or should it tighten to e.g. 2 m?
2. **`ferret/capture.py` — Y14 invalid handling.** `Y14_INVALID=0x3FFF` and
   `MIN_VALID_DEPTH_MM=50.0` are inferred (Orbbec-style all-ones sentinel); the **exact**
   raw invalid code should be confirmed from PROTOCOL.md / a live raw frame. The whole live
   RPC sequence is also worth a careful read before the first real run.
3. **`ferret/reconstruct.py` — fusion default.** `fuse()` defaults to Open3D TSDF
   (`prefer_tsdf=True`, voxel 4 mm). TSDF over-carves sparse point-sampled depth, so the
   synthetic test uses the numpy point-merge path. Decide which is the production default for
   real dense multi-view captures.
4. **`ferret/meshpipe.py` — clean_mesh targets.** Decimation target (50 000 faces) and
   hole-fill aggressiveness are tuned on synthetic geometry, not a real fused scan. Re-tune
   once you have a real mesh.
5. **`hardware/turntable/` — mechanical + step config.** Bearing recess clearance (currently
   0), marker-ring seat at 188 mm, and microstepping/steps-per-deg in `firmware/turntable.ino`.
   Review `BOM.md` and the serial protocol (`R<deg>`/`S<speed>`/`H`/`P`) before ordering parts.
6. **Dependency note.** `networkx` is NOT in the venv. meshpipe and mcp work without it
   (scipy + pymeshlab routing). If any future code relies on `trimesh.split()` /
   `trimesh.fill_holes()` directly, Setup should add `networkx`. `fast_simplification` is also
   absent, so decimation relies on the Open3D backend.
