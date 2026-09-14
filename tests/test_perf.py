"""L11 performance: evaluation, mesh and solve budgets per zoo document, and
the prefix cache paying for itself. Budgets are generous multiples of laptop
timings (bracket 0.17 s, enclosure 0.45 s, lid 0.2 s) so a CI runner passes; a
regression of several times shows up. The strict test at the end compares this
machine with tests/perf_baseline.json instead."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from plainsolid import mesh as pmesh
from plainsolid.evaluate import evaluate, solve_feature_sketch
from plainsolid.parse import parse_file

pytestmark = pytest.mark.perf

BUDGET_SECONDS = {"bracket": 1.0, "enclosure": 2.0, "lid": 1.0, "node_review": 2.0, "node": 3.0}
MESH_SECONDS = 1.5
SOLVE_SECONDS = 0.05


def _timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


@pytest.mark.parametrize("part", sorted(BUDGET_SECONDS))
def test_evaluation_and_mesh_within_budget(zoo_dir: Path, part: str):
    doc = parse_file(str(zoo_dir / f"{part}.py"))
    evaluate(doc)  # warm the import caches and the kernel
    ev, seconds = _timed(lambda: evaluate(doc))
    assert ev.has_geometry
    assert seconds < BUDGET_SECONDS[part], f"{part} evaluated in {seconds:.2f} s"
    _, mesh_seconds = _timed(lambda: pmesh.build(ev.items()))
    assert mesh_seconds < MESH_SECONDS, f"{part} meshed in {mesh_seconds:.2f} s"


def test_sketch_solves_within_budget(zoo_dir: Path):
    doc = parse_file(str(zoo_dir / "bracket.py"))
    ev = evaluate(doc)
    for name in ("profile", "holes", "slots"):
        feature = doc.feature(name)
        sg = ev.sketches[name]
        solve_feature_sketch(feature, ev.body, None, sg.plane, ev.identity())
        _, seconds = _timed(lambda f=feature, g=sg: solve_feature_sketch(f, ev.body, None, g.plane, ev.identity()))
        assert seconds < SOLVE_SECONDS, f"{name} solved in {seconds * 1000:.0f} ms"


def test_prefix_cache_makes_a_tail_edit_cheaper(zoo_dir: Path):
    doc = parse_file(str(zoo_dir / "enclosure.py"))
    full, full_seconds = _timed(lambda: evaluate(doc))
    src = (zoo_dir / "enclosure.py").read_text().replace('vents = linear_pattern("vents", vent, 6, spacing=14', 'vents = linear_pattern("vents", vent, 6, spacing=15')
    from plainsolid.parse import parse_document

    doc2 = parse_document(src, str(zoo_dir / "enclosure.py"))
    cached, cached_seconds = _timed(lambda: evaluate(doc2, cache=full))
    assert cached.cached == len(doc.features) - 1
    assert cached_seconds < full_seconds / 2, f"cached {cached_seconds:.2f} s vs full {full_seconds:.2f} s"


STRICT = os.environ.get("PLAINSOLID_PERF_STRICT") == "1"
BASELINE = Path(__file__).parent / "perf_baseline.json"


@pytest.mark.skipif(not STRICT or not BASELINE.exists(),
                    reason="opt in with PLAINSOLID_PERF_STRICT=1 to compare this machine with tests/perf_baseline.json")
def test_within_thirty_percent_of_the_recorded_baseline():
    """docs/testing.md: a number more than thirty percent over the recorded baseline fails, and a
    preview burst may not grow the process. The baseline is one laptop's, so this runs on request."""
    import importlib.util

    script = Path(__file__).parents[1] / "scripts/bench.py"
    spec = importlib.util.spec_from_file_location("bench", script)
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    baseline = json.loads(BASELINE.read_text())
    results = bench.measure_all(instances=baseline["instances"], corpus=None)
    slow = bench.regressions(results, baseline)
    assert not slow, "\n".join(slow)
