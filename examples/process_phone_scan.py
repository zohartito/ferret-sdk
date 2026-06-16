"""Turn a CrealityScan / iPhone point-cloud scan into a clean, Rhino-ready mesh
entirely in our SDK — Open3D Poisson surface reconstruction + ferret.meshpipe.

This deliberately bypasses CrealityScan's own mesher, which throws a (bogus)
"out of memory" on this Mac even with tens of GB free.

Usage:
    .venv/bin/python examples/process_phone_scan.py [path-to-point-cloud.ply]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import open3d as o3d

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from ferret import meshpipe  # noqa: E402

ART = PROJECT / "artifacts"


def main() -> int:
    default = ART / "motorcycle_pc.ply"
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    src = Path(arg) if arg and Path(arg).exists() else default  # ignore stray args (e.g. pasted '#')
    print("=" * 70)
    print("FERRET SDK  --  POINT CLOUD -> RHINO-READY MESH")
    print("=" * 70)

    pcd = o3d.io.read_point_cloud(str(src))
    print(f"loaded {len(pcd.points):,} points from {src.name}")

    # work in millimetres (Rhino-friendly); the cloud is in metres
    pcd.scale(1000.0, center=np.zeros(3))
    ext = pcd.get_axis_aligned_bounding_box().get_extent()
    print(f"  bounding box (mm) : {np.round(ext, 1)}")

    # 1. drop stray/floating points
    pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    print(f"  after denoise     : {len(pcd.points):,} points")

    # 2. oriented normals (Poisson needs them)
    diag = float(np.linalg.norm(ext))
    pcd.estimate_normals(
        o3d.geometry.KDTreeSearchParamHybrid(radius=diag * 0.02, max_nn=30))
    pcd.orient_normals_consistent_tangent_plane(30)

    # 3. Poisson surface reconstruction
    mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        pcd, depth=9)
    densities = np.asarray(densities)
    mesh.remove_vertices_by_mask(densities < np.quantile(densities, 0.06))
    mesh.compute_vertex_normals()
    raw_obj = ART / "motorcycle_mesh_raw.obj"
    o3d.io.write_triangle_mesh(str(raw_obj), mesh)
    print(f"  Poisson mesh      : {len(mesh.vertices):,} verts, "
          f"{len(mesh.triangles):,} faces -> {raw_obj.name}")

    # 4. our cleanup pipeline -> Rhino-ready
    out = ART / "motorcycle_rhino_ready.obj"
    stats = meshpipe.clean_mesh(raw_obj, out)
    print(f"  clean_mesh        : faces {stats['faces_before']:,} -> "
          f"{stats['faces_after']:,}, components {stats['components_before']} -> "
          f"{stats['components_after']}, watertight={stats['watertight']}")
    print(f"\nRHINO-READY MESH: {out}  ({out.stat().st_size:,} bytes)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
