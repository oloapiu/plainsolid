"""L2: edit operations make minimal diffs, keep comments, and reparse cleanly."""
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plainsolid import edit
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document

pytestmark = pytest.mark.roundtrip


def changed_lines(old: str, new: str) -> list[str]:
    return [l for l in edit.unified_diff(old, new).splitlines()[2:] if l[:1] in "+-"]


def comments(src: str) -> list[str]:
    return [l.strip() for l in src.splitlines() if l.strip().startswith("#")]


OPS = {
    "set_parameter": ({"op": "set_parameter", "name": "hole_d", "value": 6.5}, 2),
    "set_argument_positional": ({"op": "set_argument", "feature": "body", "kwarg": "depth", "value": {"expr": "base_depth + 5"}}, 2),
    "set_argument_new_kwarg": ({"op": "set_argument", "feature": "body", "kwarg": "flip", "value": True}, 2),
    "add_feature_extrude": ({"op": "add_feature", "kind": "extrude", "name": "boss", "args": {"sketch": "holes", "depth": 3, "draft": 2}, "after": "hole_cut"}, 1),
    "add_feature_cut_end": ({"op": "add_feature", "kind": "cut", "name": "cut2", "args": {"sketch": "holes", "through": True}}, 1),
    "add_feature_sketch": ({"op": "add_feature", "kind": "sketch", "name": "sk9", "args": {"on": "XZ", "offset": 2}}, 1),
    "add_sketch_entity": ({"op": "add_sketch_entity", "sketch": "holes", "kind": "circle", "name": "hole3", "args": {"diameter": 3, "at": [0, -10]}}, 1),
    "set_entity_argument": ({"op": "set_entity_argument", "sketch": "holes", "entity": "hole2", "kwarg": "at", "value": [22, -20]}, 2),
    "delete_sketch_entity": ({"op": "delete_sketch_entity", "sketch": "slots", "entity": "slot2"}, 1),
    "delete_feature": ({"op": "delete_feature", "feature": "slot_cut"}, 1),
}


@pytest.mark.parametrize("name", sorted(OPS))
def test_op_is_minimal_and_reparses(bracket_src, name):
    op, max_lines = OPS[name]
    new = edit.apply(bracket_src, op)
    assert len(changed_lines(bracket_src, new)) <= max_lines, changed_lines(bracket_src, new)
    assert comments(new) == comments(bracket_src)
    doc = parse_document(new)
    assert not doc.errors, [e.message for e in doc.errors]
    ev = evaluate(doc)
    assert ev.body is not None


def test_expected_text_matches_a_human(bracket_src):
    new = edit.apply(bracket_src, OPS["add_feature_extrude"][0])
    assert 'boss = extrude("boss", holes, 3, draft=2)' in new
    new = edit.apply(bracket_src, OPS["add_sketch_entity"][0])
    assert 'holes.circle("hole3", 3, at=(0, -10))' in new
    new = edit.apply(bracket_src, {"op": "add_sketch_entity", "sketch": "profile", "kind": "rect", "name": "r1", "args": {"width": 4, "height": 8, "at": [0, 0]}})
    assert 'profile.rect("r1", 4, 8)' in new


def test_geometry_changes_as_expected(bracket_src):
    base = evaluate(parse_document(bracket_src)).body.volume
    new = edit.apply(bracket_src, {"op": "set_parameter", "name": "base_depth", "value": 50})
    v = evaluate(parse_document(new)).body.volume
    # 10 mm more of the L profile (base 60x4 + wall 4x46), plus the inner fillet and minus the outer chamfer
    # running along with it; the holes stay 4 deep
    fillet_r, chamfer_d = 3.0, 2.0
    assert v - base == pytest.approx(10 * (60 * 4 + 4 * 46 + (fillet_r ** 2 - math.pi * fillet_r ** 2 / 4) - chamfer_d ** 2 / 2), rel=1e-6)


def test_delete_keeps_comment_above(bracket_src):
    new = edit.apply(bracket_src, {"op": "delete_feature", "feature": "slot_cut"})
    assert "# two vertical slots through the wall" in new


def test_errors_are_edit_errors(bracket_src):
    with pytest.raises(edit.EditError):
        edit.apply(bracket_src, {"op": "delete_feature", "feature": "nope"})
    with pytest.raises(edit.EditError):
        edit.apply(bracket_src, {"op": "set_parameter", "name": "nope", "value": 1})
    with pytest.raises(edit.EditError):
        edit.apply(bracket_src, {"op": "bogus"})
    with pytest.raises(edit.EditError):
        edit.apply(bracket_src, {"op": "set_argument", "feature": "body", "kwarg": "depth", "value": {"expr": "1 +"}})


param_values = st.floats(min_value=0.5, max_value=80, allow_nan=False, allow_infinity=False).map(lambda v: round(v, 3))
random_ops = st.lists(st.one_of(
    st.builds(lambda v: {"op": "set_parameter", "name": "thickness", "value": v}, param_values),
    st.builds(lambda v: {"op": "set_parameter", "name": "width", "value": v}, param_values),
    st.builds(lambda v: {"op": "set_argument", "feature": "body", "kwarg": "depth", "value": v}, param_values),
    st.builds(lambda x, y: {"op": "set_entity_argument", "sketch": "holes", "entity": "hole1", "kwarg": "at", "value": [x, y]},
              param_values, param_values),
    st.just({"op": "set_argument", "feature": "hole_cut", "kwarg": "flip", "value": True}),
), min_size=1, max_size=6)


@settings(max_examples=25, deadline=None)
@given(ops=random_ops)
def test_random_op_sequences_keep_the_file_parseable(ops):
    src = (__import__("pathlib").Path(__file__).parent.parent / "zoo" / "bracket.py").read_text()
    history = [src]
    for op in ops:
        src = edit.apply(src, op)
        doc = parse_document(src)
        assert not doc.errors, [e.message for e in doc.errors]
        history.append(src)
    assert comments(history[0]) == comments(history[-1])
