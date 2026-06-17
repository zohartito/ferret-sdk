"""Mesh cleanup and a watch-folder daemon for the Ferret pipeline.

``clean_mesh`` takes a raw reconstructed mesh (typically the noisy output of
TSDF fusion or point-cloud meshing) and turns it into a single, watertight-ish,
print/3D-ready solid:

    1. keep only the largest connected component (drops stray islands)
    2. remove duplicate / degenerate / unreferenced geometry
    3. fill small holes
    4. recenter to the origin and orient Z-up
    5. decimate to a target face count

trimesh is the required baseline. If Open3D or PyMeshLab are installed they are
used for the steps where they do a noticeably better job (Open3D for robust
quadric decimation, PyMeshLab for hole filling), with graceful fallback to the
trimesh-only path otherwise.

``watch_folder`` is a simple polling daemon: any new ``.ply``/``.obj``/``.stl``
that lands in ``in_dir`` is cleaned and written to ``out_dir``.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import trimesh
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

try:
    import open3d as o3d

    _HAS_OPEN3D = True
except ImportError:  # pragma: no cover - exercised only when lib missing
    _HAS_OPEN3D = False

try:
    import pymeshlab

    _HAS_PYMESHLAB = True
except ImportError:  # pragma: no cover - exercised only when lib missing
    _HAS_PYMESHLAB = False


SUPPORTED_SUFFIXES = (".ply", ".obj", ".stl", ".off", ".glb")
DEFAULT_TARGET_FACES = 50_000
# A hole is "small" if its boundary loop has at most this many edges. Larger
# openings are intentional geometry (e.g. the open base of an unclosed scan)
# and filling them would distort the model.
DEFAULT_MAX_HOLE_EDGES = 30
# Recovery hole budget used only to re-close boundaries that decimation punches
# into an already-closed surface. Decimation holes are far larger than a normal
# "small" hole, so this cap is intentionally generous to guarantee watertightness.
_DECIMATION_HOLE_RECOVERY_EDGES = 100_000


def _load_mesh(path: Path) -> trimesh.Trimesh:
    """Load a single mesh, concatenating multi-geometry scenes into one mesh."""
    loaded = trimesh.load(path, process=False, force="mesh")
    if isinstance(loaded, trimesh.Scene):
        loaded = trimesh.util.concatenate(loaded.dump())
    if not isinstance(loaded, trimesh.Trimesh) or loaded.faces.shape[0] == 0:
        raise ValueError(f"No triangle mesh found in {path}")
    return loaded


def _face_components(mesh: trimesh.Trimesh) -> tuple[int, np.ndarray]:
    """Label connected components by shared-edge face adjacency.

    Returns ``(n_components, labels)`` where ``labels`` is a per-face component
    id. Uses scipy's union-find over the face-adjacency graph so it does not
    depend on trimesh's optional ``networkx`` backend (``mesh.split`` does).
    """
    n_faces = mesh.faces.shape[0]
    if n_faces == 0:
        return 0, np.empty(0, dtype=np.int64)

    adjacency = mesh.face_adjacency  # (k, 2) face pairs sharing an edge
    if len(adjacency) == 0:
        return n_faces, np.arange(n_faces, dtype=np.int64)

    weights = np.ones(len(adjacency))
    graph = csr_matrix(
        (weights, (adjacency[:, 0], adjacency[:, 1])),
        shape=(n_faces, n_faces),
    )
    return connected_components(graph, directed=False)


def _count_components(mesh: trimesh.Trimesh) -> int:
    """Number of connected components (networkx-free)."""
    return _face_components(mesh)[0]


def _keep_largest_component(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Return the connected component with the most faces."""
    n_components, labels = _face_components(mesh)
    if n_components <= 1:
        return mesh
    largest_label = int(np.bincount(labels).argmax())
    keep_faces = mesh.faces[labels == largest_label]
    largest = trimesh.Trimesh(
        vertices=mesh.vertices, faces=keep_faces, process=False
    )
    largest.remove_unreferenced_vertices()
    return largest


def _remove_bad_geometry(mesh: trimesh.Trimesh) -> None:
    """Drop duplicate, degenerate, and unreferenced geometry in place."""
    mesh.merge_vertices()
    mesh.update_faces(mesh.unique_faces())
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    mesh.remove_infinite_values()


def _fill_holes_trimesh(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Fill holes with trimesh's built-in fan-triangulation filler.

    trimesh's filler depends on networkx; if it is not installed the mesh is
    returned unchanged rather than raising (the PyMeshLab path is preferred
    when available anyway).
    """
    try:
        mesh.fill_holes()
    except (ImportError, ModuleNotFoundError):
        pass
    return mesh


def _fill_holes_pymeshlab(
    mesh: trimesh.Trimesh, max_hole_edges: int
) -> trimesh.Trimesh:
    """Fill small holes with PyMeshLab's curvature-aware hole filler."""
    mesh_set = pymeshlab.MeshSet()
    mesh_set.add_mesh(
        pymeshlab.Mesh(
            vertex_matrix=np.asarray(mesh.vertices, dtype=np.float64),
            face_matrix=np.asarray(mesh.faces, dtype=np.int32),
        )
    )
    # PyMeshLab renamed this filter across versions; try the modern name first.
    for filter_name in ("meshing_close_holes", "close_holes"):
        try:
            mesh_set.apply_filter(filter_name, maxholesize=int(max_hole_edges))
            break
        except (pymeshlab.PyMeshLabException, RuntimeError):
            continue
    current = mesh_set.current_mesh()
    return trimesh.Trimesh(
        vertices=current.vertex_matrix(),
        faces=current.face_matrix(),
        process=False,
    )


def _decimate(mesh: trimesh.Trimesh, target_faces: int) -> trimesh.Trimesh:
    """Reduce face count to roughly ``target_faces``.

    Prefers Open3D's quadric decimation (better surface preservation); falls
    back to trimesh's simplification, and finally returns the mesh untouched if
    no decimation backend is available.
    """
    if mesh.faces.shape[0] <= target_faces:
        return mesh

    if _HAS_OPEN3D:
        o3d_mesh = o3d.geometry.TriangleMesh(
            vertices=o3d.utility.Vector3dVector(np.asarray(mesh.vertices)),
            triangles=o3d.utility.Vector3iVector(np.asarray(mesh.faces)),
        )
        simplified = o3d_mesh.simplify_quadric_decimation(
            target_number_of_triangles=int(target_faces)
        )
        return trimesh.Trimesh(
            vertices=np.asarray(simplified.vertices),
            faces=np.asarray(simplified.triangles),
            process=False,
        )

    try:
        return mesh.simplify_quadric_decimation(face_count=int(target_faces))
    except Exception:  # noqa: BLE001 - backend (fast_simplification) optional
        return mesh


def _recenter_and_orient(mesh: trimesh.Trimesh) -> None:
    """Translate centroid to origin and align the longest axis with +Z.

    The scanner produces objects sitting on a turntable, so the dominant extent
    is treated as the up axis. The mesh is modified in place.
    """
    mesh.apply_translation(-mesh.bounding_box.centroid)

    extents = mesh.extents
    up_axis = int(np.argmax(extents))
    if up_axis == 2:
        return  # already Z-up

    # Rotate the chosen axis onto +Z.
    if up_axis == 0:  # X -> Z, rotate about Y
        transform = trimesh.transformations.rotation_matrix(
            -np.pi / 2.0, [0, 1, 0]
        )
    else:  # Y -> Z, rotate about X
        transform = trimesh.transformations.rotation_matrix(
            np.pi / 2.0, [1, 0, 0]
        )
    mesh.apply_transform(transform)
    mesh.apply_translation(-mesh.bounding_box.centroid)


def clean_mesh(
    in_path: str | Path,
    out_path: str | Path,
    target_faces: int = DEFAULT_TARGET_FACES,
    max_hole_edges: int = DEFAULT_MAX_HOLE_EDGES,
) -> dict:
    """Clean a raw mesh and write a single solid to ``out_path``.

    Pipeline: largest component -> dedupe/degenerate removal -> fill small
    holes -> recenter + Z-up -> decimate to ``target_faces``.

    Returns a stats dict with before/after face counts, component counts, and
    the watertight flag, which is also useful for tests and the MCP server.
    """
    in_path = Path(in_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    mesh = _load_mesh(in_path)
    faces_before = int(mesh.faces.shape[0])
    components_before = _count_components(mesh)

    mesh = _keep_largest_component(mesh)
    _remove_bad_geometry(mesh)

    if _HAS_PYMESHLAB:
        mesh = _fill_holes_pymeshlab(mesh, max_hole_edges)
        _remove_bad_geometry(mesh)
    else:
        mesh = _fill_holes_trimesh(mesh)

    _recenter_and_orient(mesh)
    mesh = _decimate(mesh, target_faces)
    _remove_bad_geometry(mesh)

    # Quadric decimation (Open3D in particular) can punch a boundary loop into an
    # otherwise-closed surface, leaving it non-watertight (euler == 1). These
    # decimation holes are large -- far bigger than ``max_hole_edges`` -- so the
    # recovery fill uses a generous hole budget to close whatever boundary the
    # decimator created and restore a watertight, genus-0 solid. trimesh's filler
    # only closes small triangular gaps, so PyMeshLab is required for this step.
    if not mesh.is_watertight and _HAS_PYMESHLAB:
        mesh = _fill_holes_pymeshlab(mesh, _DECIMATION_HOLE_RECOVERY_EDGES)
        _remove_bad_geometry(mesh)

    components_after = _count_components(mesh)
    faces_after = int(mesh.faces.shape[0])

    mesh.export(out_path)

    return {
        "in_path": str(in_path),
        "out_path": str(out_path),
        "faces_before": faces_before,
        "faces_after": faces_after,
        "components_before": components_before,
        "components_after": components_after,
        "watertight": bool(mesh.is_watertight),
        "euler_number": int(mesh.euler_number),
    }


def watch_folder(
    in_dir: str | Path,
    out_dir: str | Path,
    poll_interval: float = 2.0,
    target_faces: int = DEFAULT_TARGET_FACES,
    max_iterations: int | None = None,
) -> list[dict]:
    """Poll ``in_dir`` and clean any new mesh into ``out_dir``.

    Runs forever by default. ``max_iterations`` bounds the number of polling
    cycles, which keeps the daemon unit-testable without threads. Returns the
    list of stats dicts for every mesh processed.
    """
    in_dir = Path(in_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    processed: set[str] = set()
    results: list[dict] = []
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        for mesh_path in sorted(in_dir.iterdir()):
            if mesh_path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if mesh_path.name in processed:
                continue
            out_path = out_dir / f"{mesh_path.stem}_clean.obj"
            try:
                stats = clean_mesh(mesh_path, out_path, target_faces=target_faces)
                results.append(stats)
            except (ValueError, OSError) as error:
                print(f"[meshpipe] skipped {mesh_path.name}: {error}")
            finally:
                processed.add(mesh_path.name)

        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            time.sleep(poll_interval)

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ferret mesh cleanup")
    parser.add_argument("input", help="input mesh (.ply/.obj/.stl)")
    parser.add_argument("output", help="output mesh path")
    parser.add_argument("--target-faces", type=int, default=DEFAULT_TARGET_FACES)
    args = parser.parse_args()

    report = clean_mesh(args.input, args.output, target_faces=args.target_faces)
    for key, value in report.items():
        print(f"{key}: {value}")
