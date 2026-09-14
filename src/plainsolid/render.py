"""Offscreen PNG rendering through pyvista. Imported lazily: vtk is slow to load."""
from __future__ import annotations

from typing import Any

import numpy as np

VIEWS = ("iso", "front", "back", "top", "bottom", "left", "right")  # the names of drawing.DIRECTIONS


class RenderError(ValueError):
    pass


HIGHLIGHT = "#ff8c1a"


def render_png(mesh: dict[str, Any], view: str = "iso", size: tuple[int, int] = (800, 600),
               highlight_faces: list[int] | None = None, color: str = "#b8bcc4",
               background: str = "white", edges: bool = True,
               highlight_edges: list[int] | None = None, highlight_vertices: list[int] | None = None) -> bytes:
    """A picture of the mesh. Highlighted faces are painted orange, highlighted
    edges drawn thick and orange, highlighted vertices as orange dots. An item
    whose colour carries an alpha below one is drawn translucent."""
    import pyvista as pv

    if view not in VIEWS:
        raise RenderError(f"unknown view {view!r}; known: {VIEWS}")
    pos = np.asarray(mesh["positions"], dtype=np.float32)
    idx = np.asarray(mesh["indices"], dtype=np.int64).reshape(-1, 3)
    if len(idx) == 0:
        raise RenderError("nothing to render")
    faces = np.hstack([np.full((len(idx), 1), 3, dtype=np.int64), idx]).ravel()
    poly = pv.PolyData(pos, faces)
    poly.cell_data["face_id"] = np.asarray(mesh["triangle_face_ids"], dtype=np.int64)

    pl = pv.Plotter(off_screen=True, window_size=list(size))
    pl.set_background(background)
    items = mesh.get("items") or []
    coloured = [it for it in items if it.get("color")]
    if coloured:
        # one actor per item colour so assemblies keep their part colours
        for it in items:
            t0, tn = it["triangles"]
            if tn == 0:
                continue
            sub = poly.extract_cells(np.arange(t0, t0 + tn))
            c = it.get("color")
            opacity = float(c[3]) if c and len(c) > 3 else 1.0
            pl.add_mesh(sub, color=tuple(c[:3]) if c else color, smooth_shading=False, show_edges=False,
                        opacity=opacity)
    else:
        pl.add_mesh(poly, color=color, smooth_shading=False, show_edges=False)
    section_faces = mesh.get("section_faces") or []
    if section_faces:
        mask = np.isin(poly.cell_data["face_id"], np.asarray(section_faces))
        if mask.any():
            pl.add_mesh(poly.extract_cells(np.flatnonzero(mask)), color="#d94f3d")
    if highlight_faces:
        mask = np.isin(poly.cell_data["face_id"], np.asarray(highlight_faces))
        if mask.any():
            pl.add_mesh(poly.extract_cells(np.flatnonzero(mask)), color=HIGHLIGHT)
    epos = np.asarray(mesh["edge_positions"], dtype=np.float32) if len(mesh["edge_positions"]) else None
    if edges and epos is not None:
        cells = _line_cells(mesh["edge_ranges"])
        if cells is not None:
            pl.add_mesh(pv.PolyData(epos, lines=cells), color="#202020", line_width=1.5)
    if highlight_edges and epos is not None:
        cells = _line_cells([mesh["edge_ranges"][i] for i in highlight_edges if i < len(mesh["edge_ranges"])])
        if cells is not None:
            pl.add_mesh(pv.PolyData(epos, lines=cells), color=HIGHLIGHT, line_width=5, render_lines_as_tubes=True)
    if highlight_vertices and len(mesh.get("vertex_positions", ())):
        vpos = np.asarray(mesh["vertex_positions"], dtype=np.float32)
        pts = vpos[[i for i in highlight_vertices if i < len(vpos)]]
        if len(pts):
            pl.add_points(pts, color=HIGHLIGHT, point_size=14, render_points_as_spheres=True)
    _set_view(pl, view)
    pl.camera.zoom(1.15)
    img = pl.screenshot(None, return_img=True)
    pl.close()
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def _line_cells(ranges) -> np.ndarray | None:
    cells: list[int] = []
    for start, count in ranges:
        if count >= 2:
            cells.append(int(count))
            cells.extend(range(int(start), int(start) + int(count)))
    return np.asarray(cells, dtype=np.int64) if cells else None


def _set_view(pl, view: str) -> None:
    """The camera sits where the drawing's view of the same name looks from: front on
    the -Y side looking along +Y with Z up, right on +X, top above with Y up, iso at
    the +X -Y +Z corner."""
    from .drawing import DIRECTIONS

    look_from, up = DIRECTIONS[view]
    pl.view_vector(look_from, up)
    pl.reset_camera()
