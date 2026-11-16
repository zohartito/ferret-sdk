"""Offline checks for the documented, reproducible installation."""

import importlib.metadata
import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]


def test_documented_python_supports_dependencies():
    readme = (ROOT / "README.md").read_text()
    version = re.search(r"python(3\.\d+) -m venv", readme).group(1)
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        requirement = Requirement(line.split("#", 1)[0].strip())
        metadata = importlib.metadata.metadata(requirement.name)
        supported = metadata.get("Requires-Python", "")
        assert version in SpecifierSet(supported), (requirement.name, version, supported)


def test_lock_covers_installed_dependency_closure():
    pins = {}
    for line in (ROOT / "requirements-lock.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        assert re.fullmatch(r"==[^*,]+", str(requirement.specifier))
        pins[requirement.name.lower().replace("_", "-")] = requirement
    pending = [Requirement(line.split("#", 1)[0].strip())
               for line in (ROOT / "requirements.txt").read_text().splitlines()]
    seen = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = requirement.name.lower().replace("_", "-")
        version = importlib.metadata.version(name)
        assert version in requirement.specifier
        assert name in pins and version in pins[name].specifier, name
        if name not in seen:
            seen.add(name)
            pending.extend(Requirement(item) for item in
                           importlib.metadata.requires(name) or [])
