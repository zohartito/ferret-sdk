"""Regression guard for tests overwriting the repository's tracked examples."""

import hashlib
import subprocess
import sys
from pathlib import Path


def test_mesh_tests_preserve_tracked_artifacts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "artifacts"], cwd=root
    ).decode().split("\0")

    def fingerprints():
        return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in paths if name}

    before = fingerprints()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/test_meshpipe.py::test_clean_mesh_produces_single_clean_solid",
         "tests/test_reconstruct.py::test_synthetic_turntable_fuse_recovers_dimensions",
         "--basetemp", str(tmp_path / "nested"),
         "-o", f"cache_dir={tmp_path / 'cache'}"],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert fingerprints() == before, "tests modified tracked artifacts"
