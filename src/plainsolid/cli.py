"""Command line: the same operations as the API, in-process by default.

Every command prints JSON so agents and scripts can consume it.
"""
from __future__ import annotations

import json
import os
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
from .stepimport import split_fragment
from .workspace import CACHE_DIR, StaleHashError, Workspace

GUIDE = Path(__file__).parent / "agent.md"
DEFAULT_PROJECT = "cad"
ROOT_HELP = f"project directory; defaults to {DEFAULT_PROJECT}/ in the plainsolid checkout"


def _checkout() -> Path | None:
    """The plainsolid checkout this tool runs from (an editable install), else None."""
    repo = Path(__file__).resolve().parents[2]
    return repo if (repo / "pyproject.toml").is_file() and (repo / "src" / "plainsolid").is_dir() else None


def _root(root: Path | None) -> Path:
    """The project directory: the argument, else `cad/` in the checkout. Never a guess
    such as the current directory, so documents cannot land somewhere by mistake."""
    if root is not None:
        return root.expanduser().resolve()
    checkout = _checkout()
    if checkout is None:
        _fail(f"no default project directory outside a plainsolid checkout: pass one, e.g. plainsolid serve ~/{DEFAULT_PROJECT}")
    return checkout / DEFAULT_PROJECT


def _open(file: Path):
    """Open a model file or a STEP file (which gets a wrapper model file);
    `x.step#node` opens one sub-assembly of the file."""
    text, node = split_fragment(str(file))
    file = Path(text)
    if not file.exists():
        _fail(f"no such file: {file}")
    file = file.resolve()
    ws = Workspace(file.parent)
    try:
        return ws, ws.open(f"{file}#{node}" if node else file)
    except ValueError as exc:
        _fail(str(exc))


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
        of: str | None = typer.Option(None, help="drawings: the part or assembly file to show, or a DXF file")) -> None:
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
def mcp(root: Path | None = typer.Argument(None, help=ROOT_HELP, show_default=False)) -> None:
    """Serve the engine to an agent over the Model Context Protocol on stdio
    (`claude mcp add plainsolid -- plainsolid mcp DIR`); `plainsolid docs` is the guide."""
    from .mcpserver import serve as _serve

    _serve(_root(root))


@app.command()
def docs() -> None:
    """Print the agent guide: the model file, selectors, edit operations, the CLI and the MCP tools."""
    typer.echo(GUIDE.read_text(encoding="utf-8"))


@app.command()
def serve(root: Path | None = typer.Argument(None, help=ROOT_HELP, show_default=False),
          port: int = 8321, host: str = "127.0.0.1",
          open: list[Path] = typer.Option([], "--open", help="documents to open at start, relative to the project"),
          idle_exit: float = typer.Option(0, "--idle-exit", help="minutes without a tab or a request after which the server stops; 0 never")) -> None:
    """Start the local API server (and the bundled client, if built) on a project
    directory, created when it does not exist yet. Documents are only ever opened
    and written inside it."""
    from .server import serve as _serve

    directory = _root(root)
    directory.mkdir(parents=True, exist_ok=True)
    _serve(directory, host=host, port=port, open_paths=[str(p) for p in open], idle_minutes=idle_exit)


# --- the running app: open, status, stop, the launcher ---------------------------

DEFAULT_PORT = 8321  # server.DEFAULT_PORT, repeated so these commands do not import the server
IDLE_MINUTES = 30    # a server that `open` starts stops this long after the last tab and request


class ServerError(Exception):
    pass


def _api(port: int, path: str, body: dict[str, Any] | None = None, timeout: float = 3.0) -> Any:
    """One request to a plainsolid server on this machine; None when nothing answers there."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if body is not None else "GET",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read())
        except ValueError:
            detail = {}
        raise ServerError(detail.get("error") or detail.get("detail") or str(exc)) from None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _health(port: int) -> dict[str, Any] | None:
    h = _api(port, "/api/health", timeout=2.0)
    return h if isinstance(h, dict) and h.get("ok") and "root" in h else None


def _start_server(directory: Path, port: int) -> dict[str, Any]:
    """Start `plainsolid serve` on `directory` detached from this terminal, logging to the
    project's cache folder, and wait until it answers."""
    import subprocess
    import sys
    import time

    log_dir = directory / CACHE_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "serve.log"
    with log_path.open("ab") as log:
        subprocess.Popen([sys.executable, "-m", "plainsolid.cli", "serve", str(directory), "--port", str(port),
                          "--idle-exit", str(IDLE_MINUTES)],
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(240):  # the kernel takes a few seconds to import
        time.sleep(0.5)
        health = _health(port)
        if health:
            return health
    _fail(f"the server did not come up on port {port}; see {log_path}")
    raise AssertionError  # unreachable


@app.command("open")
def open_files(files: list[Path] = typer.Argument(..., help="model, STEP or DXF files"),
               port: int = typer.Option(DEFAULT_PORT, help="the server's port"),
               root: Path | None = typer.Option(None, help=ROOT_HELP + "; only used to start a server when none runs")) -> None:
    """Open files in the running app, starting the server when none runs. A file inside the
    project opens as it is; a STEP or DXF file from elsewhere goes through the import dialog, which
    copies it into the project. A DXF file asks there whether it opens as a part or as a
    drawing. The browser is raised only when no tab of the app is open."""
    import urllib.parse
    import webbrowser

    paths = [f.expanduser().resolve() for f in files]
    for p in paths:
        if not p.exists():
            _fail(f"no such file: {p}")
    health = _health(port)
    started = health is None
    if health is None:
        directory = _root(root)
        directory.mkdir(parents=True, exist_ok=True)
        health = _start_server(directory, port)
    results: list[dict[str, Any]] = []
    for p in paths:
        try:
            results.append(_api(port, "/api/open-request", {"path": str(p)}))
        except ServerError as exc:
            _fail(str(exc))
    raised = not any(r["delivered"] for r in results)
    if raised:
        query = urllib.parse.urlencode([(r["action"], r["path"] if r["action"] == "open" else r["source"]) for r in results])
        webbrowser.open(f"http://127.0.0.1:{port}/?{query}")
    _out({"root": health["root"], "port": port, "started": started, "browser": raised,
          "files": [{k: v for k, v in r.items() if k != "delivered"} for r in results]})


@app.command()
def status(port: int = typer.Option(DEFAULT_PORT, help="the server's port")) -> None:
    """Whether a server runs on the port, on which project, with which documents open."""
    health = _health(port)
    if not health:
        _out({"running": False, "port": port})
        return
    docs = _api(port, "/api/documents") or []
    _out({"running": True, "port": port, "root": health["root"], "pid": health.get("pid"), "tabs": health.get("tabs"),
          "idle_minutes": health.get("idle_minutes"), "documents": [d["path"] for d in docs]})


@app.command()
def stop(port: int = typer.Option(DEFAULT_PORT, help="the server's port")) -> None:
    """Stop the server on the port; every tab of the app loses it."""
    import signal

    health = _health(port)
    if not health:
        _fail(f"no server on port {port}")
        raise AssertionError  # unreachable
    r = _api(port, "/api/shutdown", {})
    stopped = bool(isinstance(r, dict) and r.get("stopping"))
    if not stopped and health.get("pid"):  # a server run without its handle: ask the process instead
        os.kill(int(health["pid"]), signal.SIGTERM)
        stopped = True
    _out({"stopped": stopped, "root": health["root"], "pid": health.get("pid")})


def _launch_command() -> list[str]:
    """How a launcher runs this tool: the console script that is running, else python -m."""
    import sys

    exe = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if exe is not None and exe.is_file() and os.access(exe, os.X_OK) and exe.suffix != ".py":
        return [str(exe)]
    return [sys.executable, "-m", "plainsolid.cli"]


def quick_action(command: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two plists of a Finder Quick Action, "Open in plainsolid" in the right-click menu,
    that runs `command open` on every selected STEP or DXF file: document.wflow and Info.plist."""
    import shlex
    import uuid

    script = ('for f in "$@"; do\n  case "$f" in\n    *.step|*.stp|*.STEP|*.STP|*.dxf|*.DXF) '
              + " ".join(shlex.quote(c) for c in command) + ' open "$f" ;;\n  esac\ndone\n')
    uid = lambda: str(uuid.uuid4()).upper()
    defaults = [("inputMethod", 0), ("source", ""), ("CheckedForUserDefaultShell", False), ("COMMAND_STRING", ""), ("shell", "/bin/sh")]
    action = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.string"]},
        "AMActionVersion": "2.0.3",
        "AMApplication": ["Automator"],
        "AMParameterProperties": {name: {} for name, _ in defaults},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
        "ActionName": "Run Shell Script",
        "ActionParameters": {"COMMAND_STRING": script, "CheckedForUserDefaultShell": True, "inputMethod": 1,
                             "shell": "/bin/zsh", "source": ""},
        "BundleIdentifier": "com.apple.RunShellScript",
        "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False,
        "CanShowWhenRun": True,
        "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction",
        "InputUUID": uid(),
        "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
        "OutputUUID": uid(),
        "UUID": uid(),
        "UnlocalizedApplications": ["Automator"],
        "arguments": {str(i): {"default value": value, "name": name, "required": "0", "type": "0", "uuid": str(i)}
                      for i, (name, value) in enumerate(defaults)},
        "isViewVisible": 1,
        "location": "309.000000:253.000000",
        "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
    }
    workflow = {
        "AMApplicationBuild": "523", "AMApplicationVersion": "2.10", "AMDocumentVersion": "2",
        "actions": [{"action": action, "isViewVisible": 1}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {}, "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 11, "processesInput": 0,
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": 0,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    info = {"NSServices": [{
        "NSBackgroundColorName": "background",
        "NSIconName": "NSActionTemplate",
        "NSMenuItem": {"default": "Open in plainsolid"},
        "NSMessage": "runWorkflowAsService",
        "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
        "NSSendFileTypes": ["public.item"],
    }]}
    return workflow, info


def _install_quick_action(command: list[str]) -> list[Path]:
    import plistlib
    import subprocess

    bundle = Path.home() / "Library" / "Services" / "Open in plainsolid.workflow"
    (bundle / "Contents").mkdir(parents=True, exist_ok=True)
    workflow, info = quick_action(command)
    with (bundle / "Contents" / "document.wflow").open("wb") as f:
        plistlib.dump(workflow, f)
    with (bundle / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    # the Finder lists a new Quick Action only once it is ticked under its "Customize…" entry;
    # this is the record that tick writes, so the menu shows the action right away
    subprocess.run(["defaults", "write", "pbs", "NSServicesStatus", "-dict-add",
                    "(null) - Open in plainsolid - runWorkflowAsService",
                    "{ presentation_modes = { ContextMenu = 1; FinderPreview = 1; ServicesMenu = 1; TouchBar = 0; }; }"],
                   check=False, capture_output=True)
    pbs = Path("/System/Library/CoreServices/pbs")
    if pbs.exists():  # tell the Finder now rather than at the next login
        subprocess.run([str(pbs), "-update"], check=False, capture_output=True)
    return [bundle]


def desktop_entry(command: list[str]) -> tuple[str, str]:
    """A freedesktop launcher registering plainsolid for STEP and DXF files ("Open with" in the
    file manager) and the MIME package that names them: plainsolid.desktop and plainsolid.xml."""
    exe = " ".join(f'"{c}"' if " " in c else c for c in command)
    desktop = ("[Desktop Entry]\nType=Application\nName=plainsolid\nComment=Open a STEP or DXF file in plainsolid\n"
               f"Exec={exe} open %F\nTerminal=false\nMimeType=model/step;application/step;application/x-step;image/vnd.dxf;\n"
               "Categories=Graphics;Engineering;\n")
    mime = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
            '  <mime-type type="model/step">\n    <comment>STEP CAD model</comment>\n'
            + "".join(f'    <glob pattern="*.{ext}"/>\n' for ext in ("step", "stp", "STEP", "STP"))
            + '  </mime-type>\n  <mime-type type="image/vnd.dxf">\n    <comment>DXF drawing</comment>\n'
            + "".join(f'    <glob pattern="*.{ext}"/>\n' for ext in ("dxf", "DXF"))
            + "  </mime-type>\n</mime-info>\n")
    return desktop, mime


def _install_desktop_entry(command: list[str]) -> list[Path]:
    import shutil
    import subprocess

    apps = Path.home() / ".local" / "share" / "applications"
    mime = Path.home() / ".local" / "share" / "mime"
    apps.mkdir(parents=True, exist_ok=True)
    (mime / "packages").mkdir(parents=True, exist_ok=True)
    desktop, package = desktop_entry(command)
    (apps / "plainsolid.desktop").write_text(desktop, encoding="utf-8")
    (mime / "packages" / "plainsolid.xml").write_text(package, encoding="utf-8")
    for cmd in (["update-mime-database", str(mime)], ["update-desktop-database", str(apps)],
                ["xdg-mime", "default", "plainsolid.desktop", "model/step"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=False, capture_output=True)
    return [apps / "plainsolid.desktop", mime / "packages" / "plainsolid.xml"]


@app.command("install-launcher")
def install_launcher() -> None:
    """Let the file manager open STEP and DXF files with plainsolid: on macOS "Open in plainsolid"
    among the Finder's Quick Actions (right-click), on Linux a desktop entry registered
    for STEP and DXF files. Both call this very executable by its full path, so nothing else needs
    setting up; delete the written files to undo."""
    import sys

    command = _launch_command()
    if sys.platform == "darwin":
        written = _install_quick_action(command)
        note = ("in the Finder, right-click a STEP or DXF file, Quick Actions, Open in plainsolid; should the submenu "
                "show only Customize…, pick it and tick Open in plainsolid")
    elif sys.platform.startswith("linux"):
        written = _install_desktop_entry(command)
        note = "in the file manager, right-click a STEP or DXF file, Open with, plainsolid"
    else:
        _fail(f"no launcher for {sys.platform}")
        raise AssertionError  # unreachable
    _out({"command": command, "written": [str(p) for p in written], "note": note})


def main() -> None:
    app()


if __name__ == "__main__":
    main()
