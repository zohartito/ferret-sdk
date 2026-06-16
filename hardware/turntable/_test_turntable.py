"""Offline verification for the rendered turntable STL.

Asserts the STL exists, is non-trivial, parses as a valid mesh, and has the
expected approximate outer diameter and height. Run:
    .venv/bin/python hardware/turntable/_test_turntable.py
"""
from pathlib import Path

import numpy as np
import trimesh

ARTIFACTS = Path("/Users/zohartito/projects/ferret-sdk/artifacts")
STL_PATH = ARTIFACTS / "turntable.stl"

EXPECTED_DIAMETER_MM = 200.0
EXPECTED_HEIGHT_MM = 12.0  # platter_thickness(8) + ledge_height(4)
TOL_MM = 1.5  # allow for fillet rounding pulling extents slightly in


def main() -> None:
    assert STL_PATH.exists(), f"STL missing: {STL_PATH}"
    assert STL_PATH.stat().st_size > 1000, "STL is suspiciously small"

    mesh = trimesh.load(STL_PATH, force="mesh")
    n_tri = len(mesh.faces)
    assert n_tri > 100, f"mesh too trivial: {n_tri} triangles"

    extents = mesh.extents  # (x, y, z) bounding-box size in mm
    diameter_x, diameter_y, height = extents

    # Outer diameter: bounding box X and Y should both match the platter dia.
    assert abs(diameter_x - EXPECTED_DIAMETER_MM) < TOL_MM, (
        f"diameter X {diameter_x:.2f} off from {EXPECTED_DIAMETER_MM}"
    )
    assert abs(diameter_y - EXPECTED_DIAMETER_MM) < TOL_MM, (
        f"diameter Y {diameter_y:.2f} off from {EXPECTED_DIAMETER_MM}"
    )
    assert abs(height - EXPECTED_HEIGHT_MM) < TOL_MM, (
        f"height {height:.2f} off from {EXPECTED_HEIGHT_MM}"
    )

    # Roundness check: the platter is circular, so X and Y extents must agree.
    assert abs(diameter_x - diameter_y) < 0.5, "platter is not circular"

    watertight = mesh.is_watertight
    volume_cm3 = mesh.volume / 1000.0 if watertight else float("nan")

    print(f"triangles            = {n_tri}")
    print(f"vertices             = {len(mesh.vertices)}")
    print(f"diameter (X x Y) mm  = {diameter_x:.2f} x {diameter_y:.2f}")
    print(f"height mm            = {height:.2f}")
    print(f"watertight           = {watertight}")
    print(f"volume cm^3          = {volume_cm3:.2f}")
    print(f"bbox center mm       = {np.round(mesh.bounds.mean(axis=0), 2)}")
    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
