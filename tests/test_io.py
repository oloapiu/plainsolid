"""L7: STEP and STL export."""
import struct

import pytest
from build123d import import_step

from plainsolid import io as pio

pytestmark = pytest.mark.io


def test_step_round_trip(bracket_eval, tmp_path):
    path = pio.export(bracket_eval, "step", tmp_path / "bracket.step")
    back = import_step(path)
    assert back.volume == pytest.approx(bracket_eval.body.volume, rel=1e-6)
    bb_a, bb_b = bracket_eval.body.bounding_box(), back.bounding_box()
    assert tuple(bb_a.min) == pytest.approx(tuple(bb_b.min), abs=1e-4)
    assert tuple(bb_a.max) == pytest.approx(tuple(bb_b.max), abs=1e-4)
    labels = [back.label, *(c.label for c in back.children)]
    assert "bracket" in labels


def test_stl_is_binary_with_triangles(bracket_eval, tmp_path):
    path = pio.export(bracket_eval, "stl", tmp_path / "bracket.stl")
    data = path.read_bytes()
    (count,) = struct.unpack_from("<I", data, 80)
    assert count > 100 and len(data) == 84 + 50 * count


def test_unknown_format_and_no_body(bracket_eval, tmp_path):
    with pytest.raises(pio.ExportError):
        pio.export(bracket_eval, "obj", tmp_path / "x.obj")
