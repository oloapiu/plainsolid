"""The engine for agents over the Model Context Protocol: the operations of the
CLI and the HTTP API as tools on stdio, in-process on one project directory.

The files are the truth, so a GUI server watching the same directory shows every
edit an agent makes, and every tool re-reads a file the GUI changed before acting
on it. `plainsolid://guide` is the reference an agent reads first."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import compare as pcompare
from . import io as pio
from . import measure as pmeasure
from . import mesh as pmesh
from . import query as pquery
from . import refs as prefs
from . import section as psection
from .edit import EditError
from .selectors import SelectorError
from .workspace import OpenDocument, StaleHashError, Workspace

GUIDE = Path(__file__).parent / "agent.md"
INSTRUCTIONS = """plainsolid is a parametric CAD engine. A model is a Python file (a part, an
assembly or a drawing) that the tools edit with surgical patches; the file is the only
truth and re-evaluates into geometry with build123d. Units are millimetres and degrees.
Read the resource plainsolid://guide before the first edit: it lists the model file's
statements, the selector language, the JSON edit operations and a worked session.
The loop is: get_tree (or new_document), edit, read the errors the answer carries,
render or measure to check, query summary for the numbers."""

DOMAIN_ERRORS = (EditError, StaleHashError, pquery.QueryError, pmeasure.MeasureError, SelectorError,
                 pcompare.CompareError, pio.ExportError, psection.SectionError, FileNotFoundError,
                 FileExistsError, ValueError, KeyError)


class Engine:
    """The workspace behind the tools, on one project directory."""

    def __init__(self, root: str | Path | None = None):
        self.ws = Workspace(root)

    def doc(self, path: str) -> OpenDocument:
        """The document at a path (relative to the project or absolute), re-read
        from disk when something else wrote it since."""
        try:
            d = self.ws.open(path)
        except FileNotFoundError:
            raise ToolError(f"no such file: {path}") from None
        self.ws.reload_from_disk(d)
        return d

    def after_edit(self, d: OpenDocument, result: dict[str, Any]) -> dict[str, Any]:
        """What an edit did to the evaluation: every error, so a failed feature is visible at once."""
        ev = d.ensure_evaluated()
        errors = [e.to_json() for e in d.document.errors] + [e.to_json() for e in ev.errors]
        warnings = [{"feature": r.name, "message": w} for r in ev.results for w in r.warnings]
        return {**result, "ok": not errors, "errors": errors, "warnings": warnings}

    def section(self, plane: str | None, offset: float, flip: bool) -> psection.SectionSpec | None:
        return psection.parse_spec(plane, offset, flip)


def _run(fn, *args):
    """Run engine work off the event loop, turning domain errors into tool errors."""
    async def go():
        try:
            return await asyncio.to_thread(fn, *args)
        except ToolError:
            raise
        except DOMAIN_ERRORS as exc:
            raise ToolError(str(exc)) from None
    return go()


def build_server(root: str | Path | None = None) -> MCPServer:
    engine = Engine(root)
    # the mcp package configures logging; keep build123d's chatter out of the agent's stderr
    logging.getLogger("build123d").setLevel(logging.WARNING)
    server = MCPServer("plainsolid", instructions=INSTRUCTIONS, version="0.0.1", log_level="WARNING")

    @server.resource("plainsolid://guide", name="guide", mime_type="text/markdown",
                     description="The agent guide: the model file, selectors, edit operations, a worked session.")
    def guide() -> str:
        return GUIDE.read_text(encoding="utf-8")

    @server.tool()
    async def list_files() -> dict[str, Any]:
        """Model files (parts, assemblies, drawings) and STEP files under the project directory."""
        return await _run(lambda: {"root": str(engine.ws.root), "files": engine.ws.files()})

    @server.tool()
    async def new_document(path: str, kind: str = "part", name: str | None = None, material: str = "al6061",
                           of: str | None = None) -> dict[str, Any]:
        """Create a model file from the template and return its tree. kind is part, assembly,
        or drawing (then `of` names the part or assembly file it shows)."""
        return await _run(lambda: engine.ws.create(path, kind, name, material, of).tree_json())

    @server.tool()
    async def get_tree(path: str, upto: str | None = None, compact: bool = True) -> dict[str, Any]:
        """The document: meta, parameters, features with their arguments and results (ok, error
        with its line, faces created, a sketch's solution with its degrees of freedom and the
        variables still free), errors, the assembly solution or a drawing's summary. `upto`
        evaluates the file only up to that feature, as a rollback. Compact by default: the
        results appear once, under each feature, without source texts or solved coordinates;
        get_feature gives one feature in full, compact=false the whole tree."""
        def go():
            d = engine.doc(path)
            tree = d.tree_json()
            if upto:
                tree["evaluation"] = d.evaluation_for(upto).to_json()
                tree["upto"] = upto
            return _compact(tree, d) if compact else tree
        return await _run(go)

    @server.tool()
    async def get_feature(path: str, name: str) -> dict[str, Any]:
        """One feature in full: arguments and their source texts, a sketch's entities,
        constraints and solved coordinates, the result with its error, and what depends on it."""
        def go():
            d = engine.doc(path)
            tree = d.tree_json()
            for f in tree["features"]:
                if f["name"] == name:
                    return f
            raise ToolError(f"no feature named {name!r}; features: {', '.join(x['name'] for x in tree['features'])}")
        return await _run(go)

    @server.tool()
    async def get_source(path: str) -> dict[str, Any]:
        """The model file's text and its hash (pass the hash to edit for a stale check)."""
        def go():
            d = engine.doc(path)
            return {"source": d.source, "hash": d.hash, "path": str(d.path)}
        return await _run(go)

    @server.tool()
    async def edit(path: str, op: dict[str, Any], hash: str | None = None) -> dict[str, Any]:
        """Apply one JSON edit operation (see the guide: set_parameter, set_argument, add_feature,
        delete_feature, add_sketch_entity, add_constraint, batch, ...). The file is written and
        re-evaluated; the answer carries the diff, the new hash, and every error and warning of
        the evaluation. With `hash`, the edit is refused when the file changed since."""
        def go():
            d = engine.doc(path)
            return engine.after_edit(d, engine.ws.apply(d, dict(op), hash))
        return await _run(go)

    @server.tool()
    async def set_source(path: str, source: str, hash: str | None = None) -> dict[str, Any]:
        """Replace the whole model file (for rewrites too large for edit operations) and
        report the errors of the new text. Edit operations are preferable: they keep the
        rest of the file untouched."""
        def go():
            d = engine.doc(path)
            return engine.after_edit(d, engine.ws.set_source(d, source, hash))
        return await _run(go)

    @server.tool()
    async def undo(path: str) -> dict[str, Any]:
        """Undo the last edit of this session on the file."""
        def go():
            d = engine.doc(path)
            return engine.after_edit(d, engine.ws.undo(d))
        return await _run(go)

    @server.tool()
    async def redo(path: str) -> dict[str, Any]:
        """Redo the last undone edit on the file."""
        def go():
            d = engine.doc(path)
            return engine.after_edit(d, engine.ws.redo(d))
        return await _run(go)

    @server.tool()
    async def query(path: str, kind: str = "summary", upto: str | None = None, material: str | None = None,
                    density: float | None = None) -> dict[str, Any]:
        """Numbers of the geometry: summary (volume, area, bbox, counts, centre of mass, mass),
        or one of volume, area, bbox, counts, center_of_mass, mass, bom, interference."""
        def go():
            ev = engine.doc(path).evaluation_for(upto)
            kwargs = {"material": material, "density": density} if kind == "mass" else {}
            return pquery.run(ev, kind, **kwargs)
        return await _run(go)

    @server.tool()
    async def measure(path: str, a: str | dict[str, Any], b: str | dict[str, Any] | None = None,
                      upto: str | None = None, plane: str | None = None, offset: float = 0.0,
                      flip: bool = False) -> dict[str, Any]:
        """Describe one entity (a face's area, normal and centre; an edge's length, a circle's
        diameter; a vertex) or measure between two: the distance, the angle between faces or
        edges, the x, y, z components. Entities are selectors such as body.faces.top or
        hole_cut.edges.nearest((0, -20, 4)), or "point:x,y,z"; a selector must pick one entity."""
        def go():
            d = engine.doc(path)
            items = d.geometry(upto, engine.section(plane, offset, flip))
            ev = d.evaluation_for(upto)
            return pmeasure.measure(items, prefs.measure_ref(ev, items, _ref(a)),
                                    prefs.measure_ref(ev, items, _ref(b)) if b is not None else None)
        return await _run(go)

    @server.tool(structured_output=False)  # a picture and its caption, two content blocks
    async def render(path: str, view: str = "iso", width: int = 800, height: int = 600,
                     highlight: list[str] | None = None, upto: str | None = None, plane: str | None = None,
                     offset: float = 0.0, flip: bool = False, overlay: str | None = None,
                     rev: str | None = None, output: str | None = None) -> list[Any]:
        """A picture of the geometry from iso, front, back, top, bottom, left or right, with a
        caption. `highlight` paints selectors (body.faces.top, hole_cut.edges.all()) or whole
        features (a feature name) orange, so a selector can be checked before it goes into the
        file; the caption says what each highlighted entity is and whether this view shows it.
        `plane` with `offset` sections the model. `overlay` (another file) or `rev` (a git
        revision of this file) draws the comparison: common grey, added green, removed red.
        `output` also saves the PNG there."""
        from . import render as prender

        def go():
            d = engine.doc(path)
            ids: dict[str, list[int]] = {"faces": [], "edges": [], "vertices": []}
            if overlay or rev:
                _, items = engine.ws.compare(d, overlay, rev, upto)
            else:
                items = d.geometry(upto, engine.section(plane, offset, flip))
                if highlight:
                    ids = prefs.highlight_ids(d.evaluation_for(upto), items, list(highlight))
            if not items:
                raise ToolError("the document has no geometry to render")
            m = pmesh.build(items, d.cache_dir)
            png = prender.render_png(m, view, (int(width), int(height)), ids["faces"] or None,
                                     highlight_edges=ids["edges"] or None, highlight_vertices=ids["vertices"] or None)
            if output:
                out = Path(output)
                if not out.is_absolute():
                    out = engine.ws.root / out
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(png)
            if overlay or rev:
                report = engine.ws.compare(d, overlay, rev, upto)[0]
                caption = (f"compared with {rev or overlay}: +{report['added']['volume']:.1f} mm³ added (green), "
                           f"-{report['removed']['volume']:.1f} mm³ removed (red), grey is common")
            elif any(ids.values()):
                caption = prefs.describe_highlights(d.evaluation_for(upto), items, m, ids, view)["text"]
            else:
                caption = f"{view} view" + (f" up to {upto}" if upto else "") + (f", sectioned by {plane} at {offset}" if plane else "")
            return [Image(data=png, format="png"), caption]
        try:
            return await _run(go)
        except ToolError:
            raise
        except prender.RenderError as exc:
            raise ToolError(str(exc)) from None

    @server.tool()
    async def export(path: str, output: str, format: str | None = None) -> dict[str, Any]:
        """Write a part or the posed assembly as STEP or STL, a drawing as SVG, DXF or PDF.
        The format defaults to the output's suffix."""
        def go():
            d = engine.doc(path)
            out = Path(output)
            if not out.is_absolute():
                out = engine.ws.root / out
            fmt = format or out.suffix.lstrip(".")
            out.parent.mkdir(parents=True, exist_ok=True)
            pio.export(d.ensure_evaluated(), fmt, out)
            return {"path": str(out), "format": fmt}
        return await _run(go)

    @server.tool()
    async def compare(path: str, other: str | None = None, rev: str | None = None,
                      upto: str | None = None) -> dict[str, Any]:
        """What this document's geometry adds to and removes from another's (`other`, a file
        relative to this one) or from its own earlier version (`rev`, a git revision such as
        HEAD or HEAD~1): volumes, and the regions that changed with their bounding boxes."""
        def go():
            report, _ = engine.ws.compare(engine.doc(path), other, rev, upto)
            return report
        return await _run(go)

    return server


def _compact(tree: dict[str, Any], d: OpenDocument) -> dict[str, Any]:
    """The tree without its duplicates and bulk: results only under the features, no source
    texts, spans as a line, no solved coordinates or projections, a drawing as its summary."""
    out = {k: v for k, v in tree.items() if k not in ("features", "evaluation", "source")}
    out["features"] = []
    for f in tree["features"]:
        g = {k: v for k, v in f.items() if k not in ("arg_texts", "span", "dependents")}
        if f.get("span"):
            g["line"] = f["span"][0]
        r = f.get("result")
        if r and r.get("sketch"):
            g["result"] = {**r, "sketch": {k: v for k, v in r["sketch"].items() if k not in ("coords", "projected")}}
        out["features"].append(g)
    ev = tree.get("evaluation") or {}
    out["evaluation"] = {k: v for k, v in ev.items() if k not in ("results", "drawing")}
    if ev.get("drawing"):
        from . import drawing as pdrawing

        try:
            out["evaluation"]["drawing"] = pdrawing.summary(d.ensure_evaluated())
        except Exception as exc:  # noqa: BLE001 - a drawing whose model is missing still lists its views
            out["evaluation"]["drawing"] = {"error": str(exc)}
    return out


def _ref(value: str | dict[str, Any]) -> dict[str, Any] | str:
    """A measure reference from a tool argument: point:x,y,z, face:ID and friends, or a selector."""
    if isinstance(value, dict):
        return value
    kind, _, rest = value.partition(":")
    if kind == "point":
        try:
            return {"point": [float(v) for v in rest.split(",")]}
        except ValueError:
            raise ToolError(f"bad point {value!r}: use point:X,Y,Z") from None
    if kind in ("face", "edge", "vertex") and rest.isdigit():
        return {kind: int(rest)}
    return value


def serve(root: str | Path | None = None) -> None:
    """Serve on stdio until the client goes away."""
    build_server(root).run("stdio")


__all__ = ["GUIDE", "INSTRUCTIONS", "Engine", "build_server", "serve"]
