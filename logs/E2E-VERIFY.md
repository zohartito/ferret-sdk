# E2E Verification Log — ferret-sdk

- **Date:** 2026-06-15
- **Env:** `.venv` (Python 3.11.15) — open3d 0.19, pymeshlab, trimesh, numpy 2.4, mcp 1.27
- **Safety:** the live scanner/daemon was **not** touched. All verification is offline,
  against the real saved frame `probe/rpc_depth.npy` and synthetic data.

## Test suite
```
.venv/bin/python -m pytest tests/ -q   →  47 passed (2.4 s)
```

## Historical private-fixture pass — not reproducible from the public clone

The real `.npy`/`.bin` inputs and listed `.npy` artifacts below are not shipped.
As of the 2026-11-15 audit fix (FER-07), `examples/e2e_offline.py` instead uses
a labelled deterministic synthetic depth substitute when no real frame exists.
Outputs go to `artifacts/offline-demo/` or `--output-dir`. This does not reproduce
the historical real-capture measurements below.

**Stage A — real depth → 3D point cloud**
| field | value |
|---|---|
| input | `probe/rpc_depth.npy` (800×640, real captured frame) |
| intrinsics fx,fy | 478.336 px (focalPix scaled to depth res); cx=320, cy=400 |
| points | 183,412 |
| Z range (m) | 0.0512 / 1.0280 / 7.1170 (min / median / max) |
| artifact | `artifacts/e2e_real_pointcloud.ply` (2.2 MB) |

**Stage B — synthetic turntable → fuse → clean_mesh**
| field | value |
|---|---|
| views fused | 12 |
| expected box (mm) | 80 × 60 × 50 |
| measured box (mm) | 81.15 × 61.27 × 51.03 |
| rel. error | 1.4–2.1 % |
| clean_mesh | 1 component, watertight (euler = 2) |
| artifact | `artifacts/e2e_synth_clean.obj` |

## Per-component matrix
| component | tests | verified offline | needs live device |
|---|---|---|---|
| `ferret.calib` | 5/5 | intrinsics scaling; 183 412-pt cloud from real frame; metric bounds sane | absolute metric accuracy vs a measured target |
| `ferret.capture` | 9/9 | Y14 14-bit unpack round-trips exact; Y16 decode reproduces real frame byte-exact; disparity→depth self-consistent at z0; suffix discovery | live RPC capture; daemon actually emitting Y14 |
| `ferret.reconstruct` | 7/7 | real→PLY; synthetic box bbox within ~2% | multi-view fusion of a *real* turntable scan |
| `ferret.meshpipe` | 4/4 | 81 985→50 000 faces, 106→1 components, watertight | quality on real noisy scan meshes |
| `ferret_mcp.py` | 7/7 | tools registered; `depth_stats` + `reconstruct_pointcloud` + `clean_mesh` run on saved data | wiring into Claude Code; `turntable_rotate` is a stub |
| `hardware/turntable` | 14/14 | STL renders watertight (200×200×12 mm); host control vs fake serial | firmware flash + a physical print (no board/printer in loop) |

**Total: 47/47 offline tests pass. E2E offline pipeline: PASS.**
The remaining unknowns all require either the live daemon or physical hardware.
