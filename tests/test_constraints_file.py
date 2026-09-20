"""Guard against constraints.txt being silently deleted or emptied (#53) -- CI's
build-installer.yml pins the frozen sidecar's runtime deps against it, so a
missing/empty file means the installer resolves an unpinned dependency set
again with no visible failure until a build breaks."""
from pathlib import Path

CONSTRAINTS = Path(__file__).parent.parent / "constraints.txt"


def test_constraints_file_exists_and_is_not_empty():
    assert CONSTRAINTS.exists(), "constraints.txt must exist at the repo root"
    assert CONSTRAINTS.read_text(encoding="utf-8").strip(), "constraints.txt must not be empty"


def test_constraints_file_pins_every_runtime_floor():
    pins = {
        line.split("==")[0].strip().lower()
        for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.strip().startswith("#")
    }
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    deps_line = pyproject.read_text(encoding="utf-8").split("dependencies = [", 1)[1].split("]", 1)[0]
    floors = [pin.strip().strip('"') for pin in deps_line.split(",")]
    for floor in floors:
        name = floor.split(">=")[0].strip().lower()
        assert name in pins, f"{name} (a pyproject.toml runtime floor) is not pinned in constraints.txt"
