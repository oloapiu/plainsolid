"""L4 reference robustness: every selector in the zoo parts survives modest
parameter and dimension changes, and a reference deliberately broken reports
a dangling error on its own line while the body still builds."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from plainsolid import edit
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document, parse_file
from plainsolid.selectors import Selector, resolve

pytestmark = pytest.mark.refs

PARTS = ("bracket", "enclosure", "lid")


def _selectors(doc) -> list[tuple[str, Selector]]:
    """Every selector stored in feature and entity arguments, with the feature it belongs to."""
    out = []

    def walk(owner: str, value):
        if isinstance(value, dict):
            if "selector" in value:
                out.append((owner, Selector.from_json(value["selector"])))
            else:
                for v in value.values():
                    walk(owner, v)
        elif isinstance(value, list):
            for v in value:
                walk(owner, v)

    for f in doc.features:
        walk(f.name, f.args)
        for e in f.entities:
            walk(f.name, e.args)
    return out


def _resolve_at(doc, owner: str, sel: Selector):
    """Resolve a selector against the body as it stands before its owning feature."""
    idx = [f.name for f in doc.features].index(owner)
    ev = evaluate(doc, upto=doc.features[idx - 1].name)
    return resolve(sel, ev.body, ev.identity(), many=True), ev


def _fingerprint(shapes, ev) -> list[tuple[str, tuple[str, ...]]]:
    return sorted((ev.face_owner.get(hash(s.wrapped), ev.edge_owner.get(hash(s.wrapped), "")),
                   ev.face_tags.get(hash(s.wrapped), ev.edge_tags.get(hash(s.wrapped), ()))) for s in shapes)


def _numbers(src: str) -> list[tuple[str, str]]:
    """Parameter assignments and dimension values that can be nudged."""
    out = [(m.group(1), m.group(2)) for m in re.finditer(r"^(\w+) = (?:param\()?(\d+(?:\.\d+)?)", src, re.MULTILINE)]
    return out


@pytest.mark.parametrize("part", PARTS)
def test_selectors_survive_parameter_changes(zoo_dir: Path, part: str):
    src = (zoo_dir / f"{part}.py").read_text()
    doc = parse_document(src, str(zoo_dir / f"{part}.py"))
    assert not doc.errors
    baseline = evaluate(doc)
    assert all(r.ok for r in baseline.results), [r.error.message for r in baseline.results if r.error]
    sels = _selectors(doc)
    assert sels, "the part has no selectors to check"
    expected = {}
    for owner, sel in sels:
        shapes, ev = _resolve_at(doc, owner, sel)
        expected[(owner, sel.ref_name)] = (len(shapes), _fingerprint(shapes, ev), [type(s).__name__ for s in shapes])

    nudged = 0
    for name, value in _numbers(src):
        new_value = round(float(value) * 1.08, 3)
        new_src = edit.set_parameter(src, name, repr(new_value))
        new_doc = parse_document(new_src, str(zoo_dir / f"{part}.py"))
        assert not new_doc.errors, (name, new_doc.errors)
        ev = evaluate(new_doc)
        failed = [r.name for r in ev.results if not r.ok]
        assert not failed, f"{part}: {name} {value} -> {new_value} broke {failed}: " + \
            "; ".join(r.error.message for r in ev.results if r.error)
        for owner, sel in _selectors(new_doc):
            shapes, ev_at = _resolve_at(new_doc, owner, sel)
            count, fp, kinds = expected[(owner, sel.ref_name)]
            assert len(shapes) == count and [type(s).__name__ for s in shapes] == kinds, (name, sel.ref_name)
            if sel.semantic:
                assert _fingerprint(shapes, ev_at) == fp, (name, sel.ref_name)
        nudged += 1
    assert nudged >= 3


def test_dangling_reference_is_reported_on_its_line_and_the_body_still_builds(zoo_dir: Path):
    src = (zoo_dir / "bracket.py").read_text()
    # point the fillet at a sketch entity the profile does not have: its statement fails, nothing else does
    broken = src.replace('from_sketch("inner_bottom")', 'from_sketch("inner_bottm")')
    doc = parse_document(broken, str(zoo_dir / "bracket.py"))
    ev = evaluate(doc)
    r = ev.result("inner")
    assert not r.ok and "no edges labelled 'inner_bottm'" in r.error.message
    assert r.error.line == broken.splitlines().index(next(ln for ln in broken.splitlines() if "inner_bottm" in ln)) + 1
    assert all(x.ok for x in ev.results if x.name != "inner") and ev.body is not None
    assert ev.body.volume > 16000
    # delete the feature a pattern repeats: the cascade removes the pattern with it
    enc = (zoo_dir / "enclosure.py").read_text()
    after = edit.apply(enc, {"op": "delete_feature", "feature": "boss"})
    doc = parse_document(after, str(zoo_dir / "enclosure.py"))
    assert not doc.errors and doc.feature("bosses") is None and doc.feature("pilot") is None
    assert all(x.ok for x in evaluate(doc).results)


def test_click_selectors_are_stable_across_a_dimension_change(zoo_dir: Path):
    """A geometric nearest() selector written by a click keeps picking the same face
    when a dimension elsewhere changes; a semantic one does so by construction."""
    doc = parse_file(str(zoo_dir / "bracket.py"))
    ev = evaluate(doc)
    faces = ev.body.faces()
    keys = {hash(f.wrapped): i for i, f in enumerate(faces)}
    picks = []
    for f in faces:
        k = hash(f.wrapped)
        picks.append((Selector("body", "faces", (("nearest", tuple(round(c, 3) for c in f.center())),)),
                      ev.face_owner[k], ev.face_tags[k], keys[k]))
    src = (zoo_dir / "bracket.py").read_text().replace("slot_spacing = 24.0", "slot_spacing = 26.0")
    ev2 = evaluate(parse_document(src, str(zoo_dir / "bracket.py")))
    same = 0
    for sel, owner, tags, _ in picks:
        got = resolve(sel, ev2.body, ev2.identity())[0]
        if ev2.face_owner[hash(got.wrapped)] == owner and ev2.face_tags[hash(got.wrapped)] == tags:
            same += 1
    assert same >= len(picks) - 2  # only the faces the slots cut may move under a nearest() pick
    for sel_text in ("body.faces.top", "inner.faces.from_sketch('inner_bottom')", "body.faces.from_sketch('outer_wall')"):
        feature, _, rest = sel_text.partition(".")
        kind, _, op = rest.partition(".")
        arg = op[len("from_sketch('"):-2] if op.startswith("from_sketch") else op
        sel = Selector(feature, kind, (("tag", arg),))
        a = resolve(sel, ev.body, ev.identity())[0]
        b = resolve(sel, ev2.body, ev2.identity())[0]
        assert ev.face_tags[hash(a.wrapped)] == ev2.face_tags[hash(b.wrapped)]
