"""Command line: the same operations as the API, in-process by default.

Every command prints JSON so agents and scripts can consume it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from . import compare as pcompare
from . import io as pio
from . import measure as pmeasure
from . import mesh as pmesh
from . import query as pquery
from . import refs as prefs
from . import section as psection
from .edit import EditError
from .evaluate import evaluate
from .parse import parse_file
from .selectors import SelectorError
from .workspace import StaleHashError, Workspace

GUIDE = Path(__file__).parent / "agent.md"


def _open(file: Path):
    """Open a model file or a STEP file (which gets a wrapper model file)."""
    if not file.exists():
        _fail(f"no such file: {file}")
    file = file.resolve()
    ws = Workspace(file.parent)
    return ws, ws.open(file)


def _section_opt(plane: str | None, offset: float, flip: bool):
    try:
        return psection.parse_spec(plane, offset, flip)
    except psection.SectionError as exc:
        _fail(str(exc))

app = typer.Typer(help="plainsolid: parametric CAD over a Python model file", no_args_is_help=True,
                  add_completion=False)


def _out(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2))


def _fail(message: str, code: int = 1) -> None:
    typer.echo(json.dumps({"error": message}), err=True)
    raise typer.Exit(code)


def _load(file: Path):
    if not file.exists():
        _fail(f"no such file: {file}")
    doc = parse_file(str(file))
    return doc, evaluate(doc)


@app.command()
def tree(file: Path) -> None:
    """Features, parameters, errors and evaluation results as JSON. A .step file
    gets a wrapper model file next to it."""
    _, d = _open(file)
    _out(d.tree_json())


@app.command()
def new(file: Path, kind: str = typer.Option("part", help="part, assembly or drawing"),
        name: str | None = typer.Option(None, help="defaults to the file stem"),
        material: str = "al6061",
        of: str | None = typer.Option(None, help="drawings: the part or assembly file to show")) -> None:
    """Create a new model file from the template and print its tree."""
    ws = Workspace(file.resolve().parent)
    try:
        d = ws.create(file.resolve(), kind, name, material, of)
    except FileExistsError:
        _fail(f"already exists: {file}", 2)
    except ValueError as exc:
        _fail(str(exc))
    _out(d.tree_json())


@app.command()
def measure(file: Path, a: str = typer.Argument(..., help="a selector such as body.faces.top, or face:ID, edge:ID, vertex:ID, point:X,Y,Z"),
            b: str | None = typer.Argument(None), upto: str | None = None,
            plane: str | None = typer.Option(None, help="section plane XY, XZ or YZ"),
            offset: float = 0.0, flip: bool = False) -> None:
    """Describe one entity, or measure between two. Entities are named by selector
    (the same text the file uses) or by mesh id."""
    _, d = _open(file)
    items = d.geometry(upto, _section_opt(plane, offset, flip))
    ev = d.evaluation_for(upto)
    try:
        ra = prefs.measure_ref(ev, items, _ref(a))
        rb = prefs.measure_ref(ev, items, _ref(b)) if b else None
        _out(pmeasure.measure(items, ra, rb))
    except (pmeasure.MeasureError, SelectorError) as exc:
        _fail(str(exc))


def _ref(text: str) -> dict:
    kind, _, value = text.partition(":")
    if kind == "point":
        try:
            return {"point": [float(v) for v in value.split(",")]}
        except ValueError:
            _fail(f"bad point {text!r}: use point:X,Y,Z")
    if kind in ("face", "edge", "vertex") and value.isdigit():
        return {kind: int(value)}
    return {"selector": text}


def _highlights(values: list[str]) -> list[Any]:
    """`--highlight` entries: integers are face ids, "3,4" several, anything else a selector."""
    out: list[Any] = []
    for v in values:
        parts = [p.strip() for p in v.split(",")]
        if parts and all(p.isdigit() for p in parts):
            out.extend(int(p) for p in parts)
        else:
            out.append(v)
    return out


@app.command()
def query(file: Path, kind: str = typer.Argument("summary", help=f"one of {sorted(pquery.QUERIES)}"),
          material: str | None = None, density: float | None = None, upto: str | None = None) -> None:
    """Geometry queries: volume, area, bbox, counts, center_of_mass, mass, summary."""
    doc = parse_file(str(file))
    ev = evaluate(doc, upto=upto)
    kwargs = {"material": material, "density": density} if kind == "mass" else {}
    try:
        _out(pquery.run(ev, kind, **kwargs))
    except pquery.QueryError as exc:
        _fail(str(exc))


@app.command()
def edit(file: Path, op: str = typer.Argument(..., help="JSON edit operation, or @path to a JSON file"),
         dry_run: bool = typer.Option(False, help="print the diff without writing")) -> None:
    """Apply one edit operation to the file (the operation shapes are listed in docs/design.md)."""
    text = Path(op[1:]).read_text() if op.startswith("@") else op
    try:
        operation = json.loads(text)
    except json.JSONDecodeError as exc:
        _fail(f"op is not valid JSON: {exc}")
    ws = Workspace(file.parent)
    doc = ws.open(file)
    base_hash = operation.pop("hash", None)
    try:
        if dry_run:
            from . import edit as edit_ops
            new = edit_ops.apply(doc.source, operation)
            _out({"changed": new != doc.source, "diff": edit_ops.unified_diff(doc.source, new, file.name)})
        else:
            _out(ws.apply(doc, operation, base_hash))
    except EditError as exc:
        _fail(str(exc))
    except StaleHashError as exc:
        _fail(str(exc), 3)


@app.command()
def render(file: Path, output: Path = typer.Option(Path("render.png"), "-o", "--output"),
           view: str = typer.Option("iso", help="iso, front, back, top, bottom, left or right"),
           size: str = "800x600",
           highlight: list[str] = typer.Option([], "--highlight", help="a selector (body.faces.top), a feature name (its faces and edges), or face ids; repeatable"),
           upto: str | None = None, no_edges: bool = False,
           plane: str | None = typer.Option(None, help="section plane XY, XZ or YZ"),
           offset: float = 0.0, flip: bool = False,
           overlay: str | None = typer.Option(None, help="another file to compare against: common grey, added green, removed red"),
           rev: str | None = typer.Option(None, help="a git revision of this file to compare against (HEAD, HEAD~1, a branch)")) -> None:
    """Render the geometry to a PNG: optionally sectioned, with selected entities
    highlighted in orange, or as an overlay against another document."""
    from . import render as prender

    ws, d = _open(file)
    ids: dict[str, list[int]] = {"faces": [], "edges": [], "vertices": []}
    if overlay or rev:
        try:
            _, items = ws.compare(d, overlay, rev, upto)
        except pcompare.CompareError as exc:
            _fail(str(exc))
    else:
        items = d.geometry(upto, _section_opt(plane, offset, flip))
        if not items:
            _fail("the document has no geometry to render")
        try:
            ids = prefs.highlight_ids(d.evaluation_for(upto), items, _highlights(highlight))
        except SelectorError as exc:
            _fail(str(exc))
    w, h = (int(v) for v in size.lower().split("x"))
    m = pmesh.build(items, d.cache_dir)
    try:
        png = prender.render_png(m, view, (w, h), ids["faces"] or None, edges=not no_edges,
                                 highlight_edges=ids["edges"] or None, highlight_vertices=ids["vertices"] or None)
    except prender.RenderError as exc:
        _fail(str(exc))
    output.write_bytes(png)
    caption = prefs.describe_highlights(d.evaluation_for(upto), items, m, ids, view) if any(ids.values()) else None
    _out({"path": str(output), "view": view, "size": [w, h], "bytes": len(png), "highlighted": ids,
          "caption": caption["text"] if caption else None, "entities": caption["entities"] if caption else []})


@app.command()
def compare(file: Path, other: Path | None = typer.Argument(None, help="the document to compare against"),
            rev: str | None = typer.Option(None, help="instead: this file at a git revision (HEAD, HEAD~1, a branch)"),
            upto: str | None = None,
            output: Path | None = typer.Option(None, "-o", "--output", help="also render the overlay to this PNG"),
            view: str = "iso", size: str = "800x600") -> None:
    """What the geometry of this document adds to and removes from another's, by
    volume and region; with -o, a picture (common grey, added green, removed red)."""
    ws, d = _open(file)
    try:
        report, items = ws.compare(d, str(other) if other else None, rev, upto)
    except pcompare.CompareError as exc:
        _fail(str(exc))
    if output is not None:
        from . import render as prender

        w, h = (int(v) for v in size.lower().split("x"))
        try:
            png = prender.render_png(pmesh.build(items, d.cache_dir), view, (w, h))
        except prender.RenderError as exc:
            _fail(str(exc))
        output.write_bytes(png)
        report["render"] = {"path": str(output), "view": view, "size": [w, h]}
    _out(report)


@app.command()
def export(file: Path, output: Path = typer.Option(..., "-o", "--output"),
           format: str | None = typer.Option(None, help="step or stl (a drawing: svg, dxf or pdf); defaults to the output suffix")) -> None:
    """Export the body or the posed assembly to STEP or STL, a drawing to SVG, DXF or PDF."""
    _, ev = _load(file)
    fmt = format or output.suffix.lstrip(".")
    try:
        pio.export(ev, fmt, output)
    except pio.ExportError as exc:
        _fail(str(exc))
    _out({"path": str(output), "format": fmt})


@app.command()
def mesh(file: Path, output: Path = typer.Option(Path("mesh.bin"), "-o", "--output"),
         tolerance: float | None = None, upto: str | None = None,
         plane: str | None = typer.Option(None, help="section plane XY, XZ or YZ"),
         offset: float = 0.0, flip: bool = False) -> None:
    """Write the binary mesh the viewport uses."""
    _, d = _open(file)
    items = d.geometry(upto, _section_opt(plane, offset, flip))
    if not items:
        _fail("the document has no geometry")
    m = pmesh.build(items, d.cache_dir, tolerance)
    output.write_bytes(pmesh.encode(m))
    _out({"path": str(output), "triangles": len(m["indices"]) // 3, "faces": len(m["face_ranges"]),
          "items": [it["name"] for it in m["items"]]})


@app.command()
def mcp(root: Path = typer.Argument(Path("."), help="project directory")) -> None:
    """Serve the engine to an agent over the Model Context Protocol on stdio
    (`claude mcp add plainsolid -- plainsolid mcp DIR`); `plainsolid docs` is the guide."""
    from .mcpserver import serve as _serve

    _serve(root)


@app.command()
def docs() -> None:
    """Print the agent guide: the model file, selectors, edit operations, the CLI and the MCP tools."""
    typer.echo(GUIDE.read_text(encoding="utf-8"))


@app.command()
def serve(root: Path = typer.Argument(Path("."), help="project directory"),
          port: int = 8321, host: str = "127.0.0.1",
          open: list[Path] = typer.Option([], "--open", help="documents to open at start")) -> None:
    """Start the local API server (and the bundled client, if built)."""
    from .server import serve as _serve

    _serve(root, host=host, port=port, open_paths=[str(p) for p in open])


def main() -> None:
    app()


if __name__ == "__main__":
    main()
