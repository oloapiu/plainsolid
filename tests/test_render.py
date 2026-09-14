"""L8: offscreen rendering produces a real picture."""
import io

import numpy as np
import pytest
from PIL import Image

from plainsolid import mesh as pmesh
from plainsolid import render

pytestmark = pytest.mark.render
pytest.importorskip("pyvista", reason="pyvista/vtk not available on this machine")


def test_render_views_and_highlight(bracket_eval):
    m = pmesh.tessellate(bracket_eval.body)
    iso = np.asarray(Image.open(io.BytesIO(render.render_png(m, "iso", (240, 180)))))
    assert iso.shape[:2] == (180, 240)
    assert iso.std() > 10, "blank image"
    plain = np.asarray(Image.open(io.BytesIO(render.render_png(m, "top", (240, 180)))))
    every_face = list(range(len(m["face_ranges"])))
    lit = np.asarray(Image.open(io.BytesIO(render.render_png(m, "top", (240, 180), highlight_faces=every_face))))
    assert (plain != lit).any(), "highlight changed nothing"
    with pytest.raises(render.RenderError):
        render.render_png(m, "sideways")
