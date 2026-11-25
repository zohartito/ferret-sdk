"""Fresh-clone demo coverage with no private captures or hardware."""

from examples import e2e_offline
import numpy as np

from examples.synthetic_depth import make_raw_depth
from ferret import calib
from ferret.capture import decode_y16_depth_mm


def test_e2e_without_private_capture(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(e2e_offline, "REAL_DEPTH", tmp_path / "absent.npy")
    monkeypatch.setattr(e2e_offline, "ARTIFACTS", tmp_path / "outputs")
    assert e2e_offline.main() == 0
    output = capsys.readouterr().out
    assert "SYNTHETIC depth" in output
    assert "PASS" in output
    assert (tmp_path / "outputs" / "e2e_synthetic_pointcloud.ply").is_file()
    assert (tmp_path / "outputs" / "e2e_synth_clean.obj").is_file()


def test_synthetic_depth_has_known_projection_and_sentinels():
    raw = make_raw_depth()
    np.testing.assert_array_equal(raw, make_raw_depth())
    depth = decode_y16_depth_mm(raw.tobytes())
    assert depth.shape == (800, 640) and depth.dtype == np.float32
    assert depth[0, 0] == depth[400, 320] == 0.0
    intr = calib.intrinsics(640, 800)
    points = calib.depth_to_points(depth, intr)
    assert len(points) == 600 * 480 - 1
    assert 0.6 <= points[:, 2].min() < points[:, 2].max() < 0.64
