"""Shared fixtures. Every test must carry exactly one layer marker (see docs/testing.md)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ZOO = ROOT / "zoo"
LAYERS = {"unit", "golden", "roundtrip", "solver", "refs", "api", "cli", "io", "render",
          "corpus", "perf", "agent"}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--update-golden", action="store_true", default=False,
                     help="rewrite the zoo's .expected.json files from the current geometry")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    unmarked = [item.nodeid for item in items if not ({m.name for m in item.iter_markers()} & LAYERS)]
    if unmarked:
        raise pytest.UsageError("tests without a layer marker: " + ", ".join(unmarked))


@pytest.fixture(scope="session")
def zoo_dir() -> Path:
    return ZOO


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A scratch copy of the zoo to edit freely."""
    for p in ZOO.glob("*.py"):
        shutil.copy(p, tmp_path / p.name)
    shutil.copytree(ZOO / "vendor", tmp_path / "vendor")
    return tmp_path


@pytest.fixture
def bracket_src() -> str:
    return (ZOO / "bracket.py").read_text()


@pytest.fixture(scope="session")
def bracket_eval():
    from plainsolid.evaluate import evaluate
    from plainsolid.parse import parse_file

    doc = parse_file(str(ZOO / "bracket.py"))
    return evaluate(doc)


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


def load_expected(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}
