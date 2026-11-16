"""Run the documented depth probe with a fake RPC transport, never hardware."""

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

from ferret.capture import disparity_to_depth_mm, pack_y14


@pytest.mark.parametrize("pixel_format", ["Y14", "Y16"])
def test_probe_saves_explicit_metric_depth(tmp_path, monkeypatch, pixel_format):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "probe"))
    probe = importlib.import_module("ferret_rpc_depth")
    raw = np.full((800, 1280) if pixel_format == "Y14" else (800, 640),
                  872 if pixel_format == "Y14" else 1000, dtype="<u2")
    blob = pack_y14(raw) if pixel_format == "Y14" else raw.tobytes()

    class Client:
        def __init__(self, **kwargs):
            pass

        def call(self, method, *args, **kwargs):
            return [blob] if "get_frame" in method else True

        def close(self):
            pass

    monkeypatch.setattr(probe, "MsgpackRpcClient", Client)
    monkeypatch.setattr(probe, "discover_suffix", lambda client: "test")
    monkeypatch.setattr(probe, "OUT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["ferret_rpc_depth.py", "3", "24", pixel_format])
    probe.main()
    saved = np.load(tmp_path / "rpc_depth.npy")
    assert saved.shape == raw.shape
    assert saved.dtype == np.float32
    expected = disparity_to_depth_mm(raw) if pixel_format == "Y14" else raw * np.float32(0.2)
    np.testing.assert_array_equal(saved, expected)
