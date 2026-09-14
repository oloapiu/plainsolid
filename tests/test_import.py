"""L7: STEP import with hierarchy, instance names, colours and locations."""
import subprocess
import sys

import pytest

from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document, parse_file
from plainsolid.stepimport import ImportError_, import_step_tree, read_step, single_solid

pytestmark = pytest.mark.io


def test_tree_names_colours_and_positions(zoo_dir):
    tree = read_step(zoo_dir / "vendor" / "node_stub.step")
    assert tree.name == "node" and [c.name for c in tree.children] == ["box", "bracket", "glands"]
    glands = tree.children[2]
    assert [c.name for c in glands.children] == ["gland_1", "gland_2"]
    assert glands.children[0].product == glands.children[1].product  # shared geometry, distinct instances
    assert [leaf.path for leaf in tree.leaves()] == ["node.box", "node.bracket", "node.glands.gland_1", "node.glands.gland_2"]
    box, bracket, g1, g2 = tree.leaves()
    assert box.color[:3] == pytest.approx((0.75, 0.75, 0.78), abs=0.01)    # sRGB, as the generator set it
    assert bracket.color[:3] == pytest.approx((0.27, 0.51, 0.71), abs=0.01)
    assert g1.color[:3] == pytest.approx((1.0, 0.65, 0.0), abs=0.01) and g2.color[:3] == pytest.approx((0.9, 0.2, 0.1), abs=0.01)
    assert tuple(g1.shape.bounding_box().center()) == pytest.approx((-15, 10, 0), abs=1e-3)
    assert tuple(g2.shape.bounding_box().center()) == pytest.approx((15, 10, 0), abs=1e-3)
    assert g1.shape.volume == pytest.approx(g2.shape.volume)
    assert bracket.transform[1][3] == pytest.approx(-21.5)


def test_cache_by_path_mtime_size(zoo_dir):
    a = import_step_tree(zoo_dir / "vendor" / "node_stub.step")
    b = import_step_tree(zoo_dir / "vendor" / "node_stub.step")
    assert a is b


def test_single_solid_rule(zoo_dir):
    tree = read_step(zoo_dir / "vendor" / "node_stub.step")
    with pytest.raises(ImportError_, match="4 solids"):
        single_solid(tree)


def test_missing_and_bad_files(tmp_path):
    with pytest.raises(ImportError_, match="no such file"):
        read_step(tmp_path / "nope.step")
    bad = tmp_path / "bad.step"
    bad.write_text("this is not a STEP file")
    with pytest.raises(ImportError_):
        read_step(bad)


def test_assembly_document_evaluates(zoo_dir):
    doc = parse_file(str(zoo_dir / "node_review.py"))
    assert doc.kind == "assembly" and not doc.errors
    ev = evaluate(doc)
    r = ev.result("node")
    assert r.ok and r.faces_created == 22
    assert [i.name for i in ev.items()] == ["node.box", "node.bracket", "node.glands.gland_1", "node.glands.gland_2"]
    assert ev.instances[0].to_json()["children"][2]["children"][0]["path"] == "node.glands.gland_1"


def test_import_in_a_part_needs_one_solid(zoo_dir):
    src = f'from plainsolid import *\nb = import_step("b", "{zoo_dir / "vendor" / "node_stub.step"}")\n'
    ev = evaluate(parse_document(src))
    assert not ev.result("b").ok and "4 solids" in ev.result("b").error.message
    src = 'from plainsolid import *\nmeta(kind="assembly")\ns = sketch("s", on=XY)\ns.rect("r", 1, 1)\nextrude("e", s, 1)\n'
    ev = evaluate(parse_document(src))
    assert "not allowed in an assembly" in ev.result("e").error.message


def test_generator_and_step_file_agree(zoo_dir, tmp_path):
    """The committed STEP never drifts from its generator. build123d writes colour
    styles in dictionary order, so compare what the file means, not its bytes."""
    gen = zoo_dir / "vendor" / "gen_node_stub.py"
    out = tmp_path / "node_stub.step"
    code = gen.read_text().replace('OUT = Path(__file__).with_name("node_stub.step")', f'OUT = Path({str(out)!r})')
    (tmp_path / "gen.py").write_text(code)
    subprocess.run([sys.executable, str(tmp_path / "gen.py")], check=True, capture_output=True, timeout=120)
    assert _signature(read_step(out)) == _signature(read_step(zoo_dir / "vendor" / "node_stub.step"))


def _signature(tree):
    return [
        (leaf.path, leaf.product, tuple(round(c, 3) for c in (leaf.color or ())), round(leaf.shape.volume, 3),
         tuple(round(v, 3) for v in leaf.shape.bounding_box().center()))
        for leaf in tree.leaves()
    ]
