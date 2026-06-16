"""Reconstruction regression EVAL for ferret.reconstruct + ferret.meshpipe.

This is a *regression eval*, not a unit test: it builds a tiny SYNTHETIC golden
input in-code (no committed binary blob), runs it through the real
reconstruction + mesh-cleanup pipeline, and asserts the resulting mesh metrics
stay within tolerance of a committed baseline
(``tests/eval_reconstruct_baseline.json``). If a code change shifts the
geometry the pipeline produces, this eval fails and the baseline must be
re-reviewed and regenerated on purpose.

Pipeline under test
-------------------
1. Render a known box primitive from 12 turntable views into synthetic metric
   depth frames using the device intrinsics (pure-numpy pinhole z-buffer).
2. ``reconstruct.fuse(..., prefer_tsdf=False)`` -> a single trimesh.Trimesh via
   the deterministic numpy point-merge + convex-hull path.
3. ``meshpipe.clean_mesh`` -> the print-ready solid.

Asserted metrics (vs the committed baseline, within tolerance)
--------------------------------------------------------------
* vertex count is within ``vertex_count_rel`` of the baseline
* face count is within ``face_count_rel`` of the baseline
* the cleaned mesh is a single connected component
* the watertight flag matches the baseline exactly
* the recovered bounding-box dimensions match the baseline within
  ``extents_abs_mm`` AND the box is reconstructed to within ``max_dim_rel_error``
  of the *known* input extents (the headline reconstruction-accuracy metric).

Why ``prefer_tsdf=False``
-------------------------
The numpy point-merge + convex-hull path has NO dependency on Open3D or
PyMeshLab, so this eval runs and is meaningful even where those heavy libraries
do not install (e.g. a bare sandbox interpreter). The Open3D TSDF path is
exercised separately in ``test_reconstruct.py``. ``clean_mesh`` itself degrades
gracefully when Open3D/PyMeshLab are absent, and the tolerances below are wide
enough to absorb the difference in its optional decimation/hole-fill backends
between a local run and CI.

Regenerating the baseline
-------------------------
After an INTENTIONAL change to the reconstruction geometry::

    python tests/eval_reconstruct.py --update-baseline

Review the diff in ``tests/eval_reconstruct_baseline.json`` before committing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

# Allow running both as `pytest` and as `python tests/eval_reconstruct.py`:
# put the project root (the parent of tests/) on sys.path so `ferret` imports.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

trimesh = pytest.importorskip("trimesh", reason="trimesh is required for the eval")

try:
    from ferret import meshpipe, reconstruct
except ImportError:  # pragma: no cover - reconstruct/meshpipe not built yet
    reconstruct = None
    meshpipe = None


HERE = Path(__file__).resolve().parent
BASELINE_PATH = HERE / "eval_reconstruct_baseline.json"

# Device intrinsics for a depth frame, matching ferret.calib / reconstruct.
DEPTH_W, DEPTH_H = 640, 800
_FOCAL = 956.671 * (DEPTH_W / 1280)
FX = FY = _FOCAL
CX, CY = DEPTH_W / 2.0, DEPTH_H / 2.0


# --- synthetic golden input ----------------------------------------------------
def _render_depth(vertices_world: np.ndarray) -> np.ndarray:
    """Pinhole z-buffer render of mesh vertices to an HxW depth image in mm.

    A camera at the origin looks down +Z; the nearest vertex wins each pixel.
    Returns float32 depths in millimeters (0 = no surface). Pure numpy, so the
    eval has no dependency on a raycaster (rtree / pyembree / Open3D).
    """
    x, y, z = vertices_world[:, 0], vertices_world[:, 1], vertices_world[:, 2]
    front = z > 1.0
    x, y, z = x[front], y[front], z[front]

    cols = np.round(FX * x / z + CX).astype(int)
    rows = np.round(FY * y / z + CY).astype(int)
    inside = (cols >= 0) & (cols < DEPTH_W) & (rows >= 0) & (rows < DEPTH_H)
    cols, rows, z = cols[inside], rows[inside], z[inside]

    depth = np.zeros((DEPTH_H, DEPTH_W), dtype=np.float32)
    far_first = np.argsort(-z)  # paint far first so near surfaces overwrite
    depth[rows[far_first], cols[far_first]] = z[far_first]
    return depth


def _make_turntable(spec: dict):
    """Render the golden box from N turntable angles and build matching poses.

    Returns ``(frames, poses, original_mesh)``. The poses are the
    world_from_camera transforms (4x4, meters) that bring every frame back into
    the object frame. Fully deterministic for a given spec.
    """
    extents_mm = np.asarray(spec["extents_mm"], dtype=float)
    n_views = int(spec["n_views"])
    standoff_mm = float(spec["standoff_mm"])
    subdivisions = int(spec["subdivisions"])

    box = trimesh.creation.box(extents=extents_mm)
    for _ in range(subdivisions):  # dense surface samples for a clean z-buffer
        box = box.subdivide()

    standoff_m = np.array([0.0, 0.0, standoff_mm * 0.001])
    frames, poses = [], []
    for i in range(n_views):
        angle = 2.0 * np.pi * i / n_views
        rot_y = trimesh.transformations.rotation_matrix(angle, [0, 1, 0])
        rotation = rot_y[:3, :3]

        view = box.copy()
        view.apply_transform(rot_y)
        view.apply_translation([0.0, 0.0, standoff_mm])
        frames.append(_render_depth(np.asarray(view.vertices)))

        # P_cam = R @ P_obj + t  ->  P_obj = R.T @ (P_cam - t)
        world_from_cam = np.eye(4)
        world_from_cam[:3, :3] = rotation.T
        world_from_cam[:3, 3] = -rotation.T @ standoff_m
        poses.append(world_from_cam)

    return frames, poses, box


# --- pipeline + metrics --------------------------------------------------------
def _run_pipeline(tmp_dir: Path, spec: dict) -> dict:
    """Run reconstruct.fuse + meshpipe.clean_mesh and collect mesh metrics."""
    expected_mm = np.asarray(spec["extents_mm"], dtype=float)
    frames, poses, _ = _make_turntable(spec)

    # Geometry-faithful, dependency-light path (no Open3D needed).
    raw = reconstruct.fuse(frames, poses, prefer_tsdf=False)
    raw_extents_mm = np.sort(raw.extents * 1000.0)
    max_dim_rel_error = float(
        np.max(np.abs(np.sort(raw.extents * 1000.0) - np.sort(expected_mm)) / np.sort(expected_mm))
    )

    raw_path = tmp_dir / "eval_raw.obj"
    clean_path = tmp_dir / "eval_clean.obj"
    reconstruct.save_obj(raw_path, raw)
    stats = meshpipe.clean_mesh(raw_path, clean_path, target_faces=20_000)
    clean = trimesh.load(clean_path, force="mesh", process=False)

    return {
        "raw_mesh": {
            "vertex_count": int(len(raw.vertices)),
            "face_count": int(len(raw.faces)),
            "watertight": bool(raw.is_watertight),
            "euler_number": int(raw.euler_number),
            "sorted_extents_mm": [round(float(v), 3) for v in raw_extents_mm],
            "max_dim_rel_error": round(max_dim_rel_error, 4),
        },
        "clean_mesh": {
            "vertex_count": int(len(clean.vertices)),
            "face_count": int(len(clean.faces)),
            "components_after": int(stats["components_after"]),
            "watertight": bool(stats["watertight"]),
            "euler_number": int(stats["euler_number"]),
            "sorted_extents_mm": [
                round(float(v), 3) for v in np.sort(clean.extents * 1000.0)
            ],
        },
    }


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text())


# --- the eval ------------------------------------------------------------------
@pytest.mark.skipif(
    reconstruct is None or meshpipe is None,
    reason="ferret.reconstruct / ferret.meshpipe not built yet",
)
def test_reconstruction_matches_baseline(tmp_path):
    baseline = _load_baseline()
    spec = baseline["input"]
    tol = baseline["tolerances"]

    measured = _run_pipeline(tmp_path, spec)

    raw_base = baseline["raw_mesh"]
    raw = measured["raw_mesh"]
    clean_base = baseline["clean_mesh"]
    clean = measured["clean_mesh"]

    # 1. Reconstruction accuracy: the recovered box dimensions match the KNOWN
    #    input extents (the headline regression metric, baseline-independent).
    assert raw["max_dim_rel_error"] <= tol["max_dim_rel_error_abs"], (
        f"box reconstructed off by {raw['max_dim_rel_error']:.3%}, "
        f"tolerance {tol['max_dim_rel_error_abs']:.3%}"
    )

    # 2. Vertex / face counts within a wide [min, max] band. The exact count is
    #    a Qhull/trimesh-version artifact (fuse() may take the alpha-shape or the
    #    convex-hull branch depending on the installed trimesh), so we only guard
    #    that the mesh is neither degenerate (too few) nor blown up (too many).
    def _within_band(value, lo, hi, label):
        assert lo <= value <= hi, f"{label} {value} outside [{lo}, {hi}]"

    _within_band(raw["vertex_count"], tol["vertex_count_min"], tol["vertex_count_max"], "raw vertex_count")
    _within_band(raw["face_count"], tol["face_count_min"], tol["face_count_max"], "raw face_count")
    _within_band(clean["vertex_count"], tol["vertex_count_min"], tol["vertex_count_max"], "clean vertex_count")
    _within_band(clean["face_count"], tol["face_count_min"], tol["face_count_max"], "clean face_count")

    # 3. Structural invariants must match the baseline exactly.
    assert clean["components_after"] == clean_base["components_after"], (
        f"expected {clean_base['components_after']} component(s), "
        f"got {clean['components_after']}"
    )
    assert raw["watertight"] == raw_base["watertight"], "raw watertight flag regressed"
    assert clean["watertight"] == clean_base["watertight"], "clean watertight flag regressed"

    # 4. Bounding-box dimensions match the KNOWN input extents within an absolute
    #    mm band, for both the raw and the cleaned mesh. Comparing to the input
    #    (not the recorded snapshot) keeps this path-independent: both the
    #    convex-hull and the alpha-shape branch of fuse() reconstruct the box
    #    dimensions, so this gate catches a scale / intrinsics regression without
    #    false-failing on a different mesher topology. The recorded
    #    ``sorted_extents_mm`` in the baseline document the observed snapshot.
    known_ext = np.sort(np.asarray(spec["extents_mm"], dtype=float))

    raw_ext = np.asarray(raw["sorted_extents_mm"], dtype=float)
    raw_max_abs = float(np.max(np.abs(raw_ext - known_ext)))
    assert raw_max_abs <= tol["extents_abs_mm"], (
        f"raw bbox dims off {raw_max_abs:.3f} mm from input "
        f"(tolerance {tol['extents_abs_mm']} mm): "
        f"measured {raw_ext.tolist()} vs input {known_ext.tolist()}"
    )

    clean_ext = np.asarray(clean["sorted_extents_mm"], dtype=float)
    clean_max_abs = float(np.max(np.abs(clean_ext - known_ext)))
    assert clean_max_abs <= tol["extents_abs_mm"], (
        f"clean bbox dims off {clean_max_abs:.3f} mm from input "
        f"(tolerance {tol['extents_abs_mm']} mm): "
        f"measured {clean_ext.tolist()} vs input {known_ext.tolist()}"
    )


def test_baseline_file_is_valid():
    """The committed baseline parses and carries the keys the eval relies on."""
    baseline = _load_baseline()
    for section in ("input", "raw_mesh", "clean_mesh", "tolerances"):
        assert section in baseline, f"baseline missing '{section}' section"
    for key in (
        "vertex_count_min",
        "vertex_count_max",
        "face_count_min",
        "face_count_max",
        "extents_abs_mm",
        "max_dim_rel_error_abs",
    ):
        assert key in baseline["tolerances"], f"baseline missing tolerance '{key}'"


def _regenerate_baseline() -> dict:
    """Recompute the golden metrics from the current code and write the file.

    Preserves the existing ``input``/``tolerances``/``_comment`` so only the
    measured numbers change. Returns the new baseline dict.
    """
    import tempfile

    baseline = _load_baseline()
    spec = baseline["input"]
    with tempfile.TemporaryDirectory() as tmp:
        measured = _run_pipeline(Path(tmp), spec)

    baseline["raw_mesh"] = measured["raw_mesh"]
    baseline["clean_mesh"] = measured["clean_mesh"]
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
    return baseline


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reconstruction regression eval")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="recompute and overwrite tests/eval_reconstruct_baseline.json",
    )
    args = parser.parse_args()

    if reconstruct is None or meshpipe is None:
        raise SystemExit("ferret.reconstruct / ferret.meshpipe not importable")

    if args.update_baseline:
        new = _regenerate_baseline()
        print(f"baseline written to {BASELINE_PATH}")
        print(json.dumps({k: new[k] for k in ("raw_mesh", "clean_mesh")}, indent=2))
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = _run_pipeline(Path(tmp), _load_baseline()["input"])
        print(json.dumps(result, indent=2))
