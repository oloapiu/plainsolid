"""L10: real vendor STEP files and CM proposals from an untracked corpus/ directory.

Drop .step files into corpus/ at the repo root (gitignored). Every file must
import, report a hierarchy, section at three offsets, and, when a manifest
names two faces, measure the distance between them.

corpus/manifest.json (optional):
    {"node_v3.step": {"measure": [{"face": 12}, {"face": 40}], "expect_leaves": 9}}
"""
import json
import time
from pathlib import Path

import pytest

from plainsolid import measure as pmeasure
from plainsolid import mesh as pmesh
from plainsolid import section as psection
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document
from plainsolid.stepimport import read_step
from plainsolid.workspace import wrapper_source

pytestmark = pytest.mark.corpus

CORPUS = Path(__file__).resolve().parent.parent / "corpus"
FILES = sorted(p for p in CORPUS.glob("*.st*p")) if CORPUS.is_dir() else []

if not FILES:
    pytest.skip("no corpus/ directory with STEP files (local-only test data)", allow_module_level=True)


def manifest() -> dict:
    p = CORPUS / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_corpus_file(path):
    entry = manifest().get(path.name, {})
    t = time.perf_counter()
    tree = read_step(path)
    read_s = time.perf_counter() - t
    leaves = [leaf for leaf in tree.leaves() if leaf.shape is not None]
    assert leaves, "no geometry"
    if "expect_leaves" in entry:
        assert len(leaves) == entry["expect_leaves"]
    assert len({leaf.path for leaf in leaves}) == len(leaves), "instance paths must be unique"

    # the wrapper document the GUI would create
    doc = parse_document(wrapper_source(path.name), str(path.with_suffix(".py")))
    assert not doc.errors
    ev = evaluate(doc)
    assert all(r.ok for r in ev.results), [r.error.message for r in ev.results if r.error]
    items = ev.items()
    t = time.perf_counter()
    m = pmesh.build(items)
    mesh_s = time.perf_counter() - t
    assert m["triangles"] if "triangles" in m else len(m["indices"]) > 0

    lo, hi = m["bbox"][0][2], m["bbox"][1][2]
    for frac in (0.25, 0.5, 0.75):
        spec = psection.parse_spec("XY", lo + frac * (hi - lo), False)
        cut = psection.apply(items, spec)
        assert cut, f"section at {frac} removed everything"
        assert pmesh.build(cut)["section_faces"], f"section at {frac} produced no cap faces"

    if "measure" in entry:
        a, b = entry["measure"]
        result = pmeasure.measure(items, a, b)
        assert result["distance"] >= 0
        if "expect_distance" in entry:
            assert result["distance"] == pytest.approx(entry["expect_distance"], abs=1e-3)

    print(f"\n{path.name}: {len(leaves)} leaves, {len(m['face_ranges'])} faces, "
          f"read {read_s:.2f}s, mesh {mesh_s:.2f}s")
