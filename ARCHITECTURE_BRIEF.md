# ARCHITECTURE_BRIEF — one frame, end to end (gauntlet cold read)

How one depth frame travels from the daemon's msgpack-RPC bytes to an MCP tool
response. All refs are `file:line` on `main` (4f04e08).

## 1. RPC bytes → decoded metric frame
`FerretCamera.open()` (ferret/capture.py:229) connects `MsgpackRpcClient`
(ferret/rpc_client.py:43) to 127.0.0.1:10010 — a **raw msgpack stream, no length
prefix** (rpclib wire format): request `[0, msgid, method, params]`, response
`[1, msgid, error, result]` (rpc_client.py:8-11). `call()` (rpc_client.py:71)
packs with `msgpack.packb(..., use_bin_type=True)`, sends, matches responses by
msgid while skipping interleaved notifies (:80-91); `_next_obj` (:56) pulls
whole objects off a streaming `msgpack.Unpacker` under a deadline.

Suffix discovery: methods are registered as `<base><suffix>`, the depth camera's
USB path minus the dot (`1-2-1.0` → `1-2-10`); `_candidate_suffixes`
(capture.py:178) parses `/Library/Creality/Log/ScannerStreamSDK.log`, and
`_discover_suffix` (capture.py:296) probes candidates with
`ob_get_stream_profile_list<suffix>()`, accepting the first that doesn't answer
rpclib's `"could not find function"` (capture.py:308).

Frame grab: `_start_stream(OB_SENSOR_DEPTH=3, fmt)` (capture.py:315) calls
`ob_rpc_start_stream<suffix>(3, fmt, 1280, 800, 30)`. `_grab_frame`
(capture.py:321) polls `ob_rpc_get_frame<suffix>()`; the frameset is
`[a, ts, seq, 255, <bytesA>, sizeA, <bytesB>, sizeB, <bytesC>, sizeC, idx]`
(PROTOCOL.md:53), and the largest byte slot >1000 B *is* the image
(capture.py:328-331). `get_depth_mm` (capture.py:263) hands it to
`_decode_depth` (capture.py:281), which dispatches on byte length:

- **1,792,000 B = raw Y14 disparity** (1280×800×14/8): `unpack_y14`
  (capture.py:83) rebuilds each 56-bit little-endian 7-byte group and slices
  four 14-bit pixels at bit offsets [0:14]/[14:28]/[28:42]/[42:56];
  `disparity_to_depth_mm` (capture.py:137) computes
  `depth_mm = baseline·focal_pix / (raw·unit − disp_offset)`
  = 149.79·956.671/(raw·0.5−218), zeroing bad terms + the sentinel.
- **even size = computed Y16**: `decode_y16_depth_mm` (capture.py:161) does
  `<u2` → reshape via `_Y16_SHAPE_TABLE` (1,024,000 B → 800×640; else a 640-wide
  heuristic) → `mm = value · DEPTH_VALUE_SCALE (0.2)`, sentinels 0/0xFFFF→0.
## 2. The numeric story (where each constant lives)

- **Y16 ×0.2 mm scale** — PROTOCOL.md:66 ("mm = u16 * 0.2"); constants:
  capture.py:55, calib.py:31, ferret_mcp.py:26; applied at capture.py:172,
  calib.py:41, ferret_mcp.py:86. Raw 65535 would masquerade as 13107 mm, so
  calib's range gate `[0.05, 8.0] m` (calib.py:40-41) rejects it — only the 0
  sentinel needs an explicit test (calib.py:94).
- **Y14 unpack** — capture.py:83-111 (packer inverse :114-134); round-trip
  proven bit-exact in tests/test_capture.py:65-72 over a full random frame.
- **0x3FFF sentinel** — 14-bit all-ones = "no return" in raw disparity;
  `Y14_INVALID` capture.py:57, rejected by `(raw < Y14_INVALID)` in the valid
  mask capture.py:154. Without it, raw 16383 → disp 7973.5 → ≈18 mm bogus near
  point — exactly the regression tests/test_capture.py:148-152 pins.
- **disparity→depth via z0** — PROTOCOL.md:74-84 logs baseline/focal/z0/
  dispOffset/unit. z0=657.34 mm is the reference depth: baseline·focal/z0 ≈ 218.0
  == disp_offset, so raw≈872 maps to z0 — stated as a numerical coincidence of
  the logged constants in the docstring capture.py:143-145 and asserted at
  tolerance in tests/test_capture.py:135-146 (`depth ≡ z0` when
  raw·unit−disp_offset = baseline·focal/z0).

## 3. Calibrated points

`get_depth_mm` returns float32 mm (0 = invalid). `ferret.calib.intrinsics(w,h)`
(calib.py:57) scales the native-IR focal 956.671 px by `width/1280` (640-wide
depth ⇒ fx=fy=478.3355), principal point = centre. `depth_to_points`
(calib.py:100) keeps pixels passing `valid_mask` (finite, ≠0, ∈[0.05, 8] m,
calib.py:78) and back-projects x=(u−cx)z/fx, y=(v−cy)z/fy, z=mm/1000 → Nx3 float32
METRES (+X right, +Y down, +Z forward). `reconstruct._project`
(reconstruct.py:63) defers to this same function so all modules share one truth.

## 4. TSDF fusion → cleaned mesh

`reconstruct.fuse` (reconstruct.py:196): Open3D path `_fuse_open3d`
(reconstruct.py:229) builds `ScalableTSDFVolume(voxel_length=voxel_mm/1000,
sdf_trunc=5·voxel, NoColor)`, converts each frame to metres, wraps it as RGBD
(`depth_scale=1.0`, trunc 10 m), integrates with `extrinsic =
inv(world_from_camera)` (poses are world-from-camera per the turntable
convention reconstruct.py:207-209), extracts + normals → `trimesh.Trimesh`.
Without Open3D — or after any TSDF hiccup (:219-226) — it merges all posed
clouds (`_merge_points` :149) and surfaces them with an alpha shape, falling
back to convex hull (`_mesh_from_points` :168).

`meshpipe.clean_mesh` (meshpipe.py:238) then: keep largest face-adjacency
component (:115), merge/degenerate/duplicate removal (:129), small-hole fill ≤
`max_hole_edges` (PyMeshLab preferred :152, trimesh fallback :138), recenter +
longest-axis-to-Z (:212), quadric decimation preferring fast_simplification
(:178), then a generous-budget recovery fill for holes the decimator punched
(:291-293); exports OBJ + stats dict (:302-311).

## 5. MCP tool response

`ferret_mcp.py` (FastMCP, 5 tools): `reconstruct_pointcloud` (ferret_mcp.py:388)
loads a `.npy` via `_load_depth_mm` (:51; mmap header validation, u16→mm with
0/65535 zeroed), calls `reconstruct.depth_to_pointcloud` (:101), and streams a
bounded binary PLY into `artifacts/` (`_write_ply_exclusive` :150; O_EXCL,
dirfd-relative, byte-capped). `clean_mesh` (:421) runs meshpipe inside a spawn
worker under CPU/AS/FSIZE rlimits + timeout kill escalation (:277).
`depth_stats` (:354) returns coverage + p25/p50/p75 over valid pixels. The loop
an agent sees: RPC frameset → decode → mm → back-project (m) → fuse/clean →
JSON stats + artifact paths.

## 6. Why eval_gate exists outside pytest's glob

pytest collects `test_*.py`/`*_test.py`; `tests/eval_reconstruct.py` matches
neither, so `pytest -q` (PR CI) never runs the golden-baseline reconstruction
regression — it is a *regression eval*, not a unit test (:1-9).
`tests/eval_gate.py:6-9` runs it explicitly as a push gate
(.github/workflows/eval-gate.yml) against the committed
`tests/eval_reconstruct_baseline.json`. Regenerating that baseline
(`--update-baseline`) would silently hide geometry drift — a scale/intrinsics
regression or a mesher switch becomes the new "truth"; only the accuracy check
vs KNOWN input extents (max_dim_rel_error, :206) survives regeneration, which
is why baseline edits are INTENTIONAL-only events (AGENTS.md "Loops").

## 7. The gap this gauntlet closes

Everything above is verified offline except the live RPC path itself:
`FerretCamera.open/get_depth_mm` were refactored verbatim from the proven probe
but never executed (README.md "Live-device caveat") because ports 10010-10013
own real hardware. fakeferret re-creates the daemon on 20010+ so the full
bytes→decode→geometry chain runs unmodified, with `probe/rpc_depth.npy`
as golden replay oracle.
