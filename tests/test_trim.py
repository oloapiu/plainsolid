"""Round-trip: the trim operation on lines, arcs and circles through the workspace, and
per-side construction on rect and polygon macros."""
import math

import pytest

from plainsolid import edit
from plainsolid.parse import parse_document
from plainsolid.sketchgeom import profile_edges
from plainsolid.workspace import Workspace

CROSS = '''from plainsolid import *
s = sketch("s", on=XY)
s.line("a", (0, 0), (40, 0))
s.line("b", (20, -10), (20, 10))
s.line("c", (30, -10), (30, 10))
s.circle("h", 10, at=(10, 0))
s.length("la", "a", 40)
s.point("p", (20, 10))
s.coincident("tie", "b.end", "p")
'''


def write(tmp_path, text, name="part.py"):
    p = tmp_path / name
    p.write_text(text)
    return p


@pytest.mark.roundtrip
def test_trim_cuts_a_line_to_its_crossings_and_relates_the_cut_ends(tmp_path):
    ws = Workspace(tmp_path)
    doc = ws.open(write(tmp_path, CROSS).name)
    # the middle of a, between b and c: two colinear pieces, each end on the line it was cut at; the length goes
    r = ws.apply(doc, {"op": "trim", "sketch": "s", "entity": "a", "at": [25, 0.2]}, doc.hash)
    src = doc.source
    assert r["changed"] and not r["solution"]["conflicting"]
    assert 's.line("a", (0, 0), (20, 0))' in src and 's.line("line1", (30, 0), (40, 0))' in src
    assert 's.coincident("c1", "a.end", "b")' in src and 's.coincident("c2", "line1.start", "c")' in src
    assert 's.colinear("cl1", "line1", "a")' in src and 's.length("la"' not in src
    # b above a: only the crossing below the cursor bounds it, the piece to the top goes; the tie at the removed end goes
    ws.apply(doc, {"op": "trim", "sketch": "s", "entity": "b", "at": [20, 8]}, doc.hash)
    src = doc.source
    assert 's.line("b", (20, -10), (20, 0))' in src and 's.coincident("c3", "b.end", "a")' in src and '"tie"' not in src
    # c has crossings with a only (at t=0.5): trimming below leaves the top piece
    ws.apply(doc, {"op": "trim", "sketch": "s", "entity": "c", "at": [30, -8]}, doc.hash)
    assert 's.line("c", (30, 0), (30, 10))' in doc.source
    # nothing crosses line1 any more except c at its start... a piece with no crossing goes entirely
    ws.apply(doc, {"op": "trim", "sketch": "s", "entity": "line1", "at": [35, 0]}, doc.hash)
    assert '"line1"' not in doc.source and '"cl1"' not in doc.source and '"c2"' not in doc.source
    assert not parse_document(doc.source).errors


@pytest.mark.roundtrip
def test_trim_turns_a_circle_into_the_arc_that_stays_and_refuses_macros(tmp_path):
    ws = Workspace(tmp_path)
    doc = ws.open(write(tmp_path, CROSS + 's.rect("r", 10, 10, at=(60, 0))\ns.diameter("dh", "h", 10)\n').name)
    # the top of the circle, between its crossings with a at (5, 0) and (15, 0): the bottom half stays, same name
    r = ws.apply(doc, {"op": "trim", "sketch": "s", "entity": "h", "at": [10, 5]}, doc.hash)
    src = doc.source
    assert r["changed"] and 's.arc("h", (10, 0), (5, 0), (15, 0))' in src and 's.circle("h"' not in src
    assert 's.coincident("c1", "h.start", "a")' in src and 's.coincident("c2", "h.end", "a")' in src and 's.diameter("dh", "h", 10)' in src
    assert not r["solution"]["conflicting"]
    with pytest.raises(edit.EditError, match="cannot be trimmed"):
        ws.apply(doc, {"op": "trim", "sketch": "s", "entity": "r", "at": [55, 0]}, doc.hash)
    # an arc: cut in the middle by b... the arc h runs under a from (5,0) to (15,0); nothing crosses it now, so it goes whole
    ws.apply(doc, {"op": "trim", "sketch": "s", "entity": "h", "at": [10, -5]}, doc.hash)
    assert '"h"' not in doc.source and '"dh"' not in doc.source


@pytest.mark.roundtrip
def test_construction_sides_of_a_macro(tmp_path):
    src = ('from plainsolid import *\ns = sketch("s", on=XY)\ns.rect("r", 40, 20, construction=["top"])\n'
           's.line("lid", (-20, 10), (20, 10))\ns.fix("f", "r")\nbody = extrude("body", s, 3)\n')
    doc = parse_document(src)
    assert not doc.errors, [e.message for e in doc.errors]
    e = doc.feature("s").entity("r")
    assert e.construction is False and e.construction_sides == ("top",) and e.to_json()["construction_sides"] == ["top"]
    labels = [lbl for _, lbl in profile_edges(doc.feature("s"))]
    assert "r.top" not in labels and {"r.bottom", "r.left", "r.right", "lid"} <= set(labels)
    ws = Workspace(tmp_path)
    d = ws.open(write(tmp_path, src).name)
    ev = d.ensure_evaluated()
    assert not ev.errors and ev.body.volume == pytest.approx(40 * 20 * 3)  # the lid line closes the open rect
    ws.apply(d, {"op": "set_entity_argument", "sketch": "s", "entity": "r", "kwarg": "construction", "value": ["top", "left"]}, d.hash)
    assert 's.rect("r", 40, 20, construction=["top", "left"])' in d.source
    ws.apply(d, {"op": "set_entity_argument", "sketch": "s", "entity": "r", "kwarg": "construction", "value": []}, d.hash)
    assert 's.rect("r", 40, 20)' in d.source
    bad = parse_document(src.replace('["top"]', '["roof"]'))
    assert bad.errors and "unknown side" in bad.errors[0].message
    whole = parse_document(src.replace('["top"]', '["top", "bottom", "left", "right"]'))
    assert whole.feature("s").entity("r").construction is True and whole.feature("s").entity("r").construction_sides == ()
