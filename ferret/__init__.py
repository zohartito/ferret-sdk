"""Ferret 3D-scanning SDK.

Offline pipeline for the Ferret depth scanner: depth-to-point-cloud
reconstruction, mesh cleanup, capture (Y16/Y14 over msgpack-RPC), and an
MCP server exposing the pipeline as tools.

Submodules:
    calib       camera intrinsics and depth -> metric point projection
    capture     FerretCamera RPC client (Y16/Y14)
    reconstruct point-cloud / TSDF fusion and PLY/OBJ export
    meshpipe    mesh cleanup and watch-folder daemon
"""

__version__ = "0.1.0"

__all__ = [
    "calib",
    "capture",
    "reconstruct",
    "meshpipe",
]
