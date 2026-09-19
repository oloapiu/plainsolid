"""The local API server. The browser client and the CLI's --server mode are
strict clients of this. One server per project directory, localhost only."""
from __future__ import annotations

import asyncio
import functools
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import compare as pcompare
from . import io as pio
from . import measure as pmeasure
from . import mesh as pmesh
from . import query as pquery
from . import refs as prefs
from . import render as prender
from . import section as psection
from . import views as pviews
from .edit import EditError
from .operations import EditOperation, EditRequest
from .selectors import SelectorError
from .workspace import OpenDocument, StaleHashError, Workspace

DEFAULT_PORT = 8321
KERNEL_WORKERS = 1  # kernel threads per server; see create_app
_render_lock = threading.Lock()


class OpenRequest(BaseModel):
    path: str


class LocalFileRequest(BaseModel):
    path: str  # an absolute path on this machine, from `plainsolid open` or a launcher


class ImportRequest(BaseModel):
    source: str  # an absolute path to a STEP file, copied into the project
    folder: str = ""
    name: str | None = None


class NewRequest(BaseModel):
    path: str
    kind: str = "part"
    name: str | None = None
    material: str = "al6061"
    of: str | None = None  # drawings: the model file, relative to the project root


class SourceRequest(BaseModel):
    source: str
    hash: str | None = None


class SectionRequest(BaseModel):
    plane: str | None = None
    offset: float = 0.0
    flip: bool = False
    origin: list[float] | None = None
    normal: list[float] | None = None

    def spec(self) -> psection.SectionSpec | None:
        return psection.parse_spec(self.plane, self.offset, self.flip, self.origin, self.normal)


class RenderRequest(BaseModel):
    view: str = "iso"
    width: int = 800
    height: int = 600
    highlight: list[int | str] = []  # face ids, or selectors such as body.faces.top, or feature names
    upto: str | None = None
    edges: bool = True
    section: SectionRequest | None = None
    overlay: str | None = None  # another document to compare against: common grey, added green, removed red
    rev: str | None = None  # or this document at a git revision


class LocateRequest(BaseModel):
    refs: list[int | str]  # selectors such as body.faces.top, feature names, or face ids
    upto: str | None = None


class MeasureRequest(BaseModel):
    a: dict[str, Any] | str  # {"face": id} and friends, or a selector
    b: dict[str, Any] | str | None = None
    upto: str | None = None
    section: SectionRequest | None = None


class CompareRequest(BaseModel):
    other: str | None = None  # a document, relative to this one
    rev: str | None = None  # or this document at a git revision
    upto: str | None = None


class ViewsRequest(BaseModel):
    views: dict[str, Any]


class SolveRequest(BaseModel):
    sketch: str
    drag: dict[str, list[float]] | None = None


class ExportRequest(BaseModel):
    format: str
    path: str


class PreviewRequest(BaseModel):
    op: EditOperation
    choose_flip: bool = False


class DragRequest(BaseModel):
    instance: str
    poses: dict[str, Any] | None = None
    translate: list[float] | None = None
    rotate: list[float] | None = None


def create_app(root: str | Path | None = None, serve_client: bool = True) -> FastAPI:
    ws = Workspace(root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(_watch(app, ws)), asyncio.create_task(_idle(app))]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    app = FastAPI(title="plainsolid", version="0.0.1", lifespan=lifespan)
    app.state.workspace = ws
    app.state.subscribers: dict[str, set[asyncio.Queue]] = {}
    app.state.watchers: set[asyncio.Queue] = set()  # one per open tab: the workspace socket
    app.state.loop = None
    app.state.server = None          # the uvicorn server, when serve() runs one: shutdown and idle exit
    app.state.port = None
    app.state.idle_minutes = 0.0     # stop after this long with no tab and no request; 0 never
    app.state.last_activity = time.monotonic()
    # OCCT is not safe to use from two threads on shapes that documents share through the part
    # cache, so every kernel operation (evaluating, meshing, previewing, solving, exporting) runs
    # on this one worker; requests queue behind it in arrival order and the event loop stays free
    app.state.kernel = ThreadPoolExecutor(max_workers=KERNEL_WORKERS, thread_name_prefix="kernel")

    async def run(fn, *args):
        return await asyncio.get_running_loop().run_in_executor(app.state.kernel, functools.partial(fn, *args))

    def _doc(doc_id: str) -> OpenDocument:
        try:
            return ws.get(doc_id)
        except KeyError:
            raise HTTPException(404, f"no open document {doc_id!r}") from None

    def _listener(doc_id: str, payload: dict[str, Any]) -> None:
        loop = app.state.loop
        if loop is None:
            return
        for q in list(app.state.subscribers.get(doc_id, ())):
            loop.call_soon_threadsafe(q.put_nowait, payload)

    ws.listeners.append(_listener)

    @app.middleware("http")
    async def _activity(request: Request, call_next):
        app.state.last_activity = time.monotonic()
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def _request_error(_: Request, exc: RequestValidationError):
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return JSONResponse({"error": f"invalid request: {details}"}, status_code=422)

    @app.exception_handler(EditError)
    async def _edit_error(_: Request, exc: EditError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(pquery.QueryError)
    async def _query_error(_: Request, exc: pquery.QueryError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(prender.RenderError)
    async def _render_error(_: Request, exc: prender.RenderError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(psection.SectionError)
    async def _section_error(_: Request, exc: psection.SectionError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(pmeasure.MeasureError)
    async def _measure_error(_: Request, exc: pmeasure.MeasureError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(pio.ExportError)
    async def _export_error(_: Request, exc: pio.ExportError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(StaleHashError)
    async def _stale(_: Request, exc: StaleHashError):
        return JSONResponse({"error": str(exc), "hash": exc.expected}, status_code=409)

    @app.exception_handler(OSError)
    async def _file_error(_: Request, exc: OSError):
        return JSONResponse({"error": f"file operation failed: {exc}"}, status_code=500)

    @app.exception_handler(SelectorError)
    async def _selector_error(_: Request, exc: SelectorError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(pcompare.CompareError)
    async def _compare_error(_: Request, exc: pcompare.CompareError):
        return JSONResponse({"error": str(exc)}, status_code=400)


    # --- documents -------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "root": str(ws.root), "documents": len(ws.docs), "pid": os.getpid(),
                "port": app.state.port, "tabs": len(app.state.watchers), "idle_minutes": app.state.idle_minutes}

    @app.post("/api/shutdown")
    def shutdown() -> dict[str, Any]:
        """Stop the server: `plainsolid stop` and the file menu's "quit server"."""
        server = app.state.server
        if server is None:
            return {"stopping": False, "root": str(ws.root)}
        server.should_exit = True
        return {"stopping": True, "root": str(ws.root)}

    @app.get("/api/documents")
    def list_documents() -> list[dict[str, Any]]:
        return [{"id": d.id, "path": str(d.path), "hash": d.hash, "kind": d.document.kind,
                 "name": d.document.meta.get("name") or d.path.stem} for d in ws.docs.values()]

    @app.get("/api/files")
    def list_files() -> dict[str, Any]:
        """Model files and STEP files under the project, for choosers."""
        return {"root": str(ws.root), "files": ws.files()}

    @app.post("/api/documents/{doc_id}/close")
    def close_document(doc_id: str) -> dict[str, Any]:
        _doc(doc_id)
        ws.close(doc_id)
        app.state.subscribers.pop(doc_id, None)
        return {"closed": doc_id, "documents": list_documents()}

    @app.post("/api/documents/open")
    async def open_document(req: OpenRequest) -> dict[str, Any]:
        try:
            doc = ws.open(req.path)
        except FileNotFoundError:
            raise HTTPException(404, f"no such file: {req.path}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return await run(doc.tree_json)

    def _imported(fn) -> OpenDocument:
        try:
            return fn()
        except FileExistsError as exc:
            raise HTTPException(409, f"already exists: {exc}") from None
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such file: {exc}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/documents/import")
    async def import_document(req: ImportRequest) -> dict[str, Any]:
        """Copy a STEP file from elsewhere on this machine into the project and open it."""
        src = Path(req.source).expanduser()
        doc = await asyncio.to_thread(_imported, functools.partial(
            ws.import_file, req.folder, req.name or src.stem, src.suffix, source=src))
        return await run(doc.tree_json)

    @app.put("/api/documents/upload")
    async def upload_document(request: Request, folder: str = "", name: str = "", suffix: str = "") -> dict[str, Any]:
        """The same from the browser: the bytes of a dropped STEP file."""
        data = await request.body()
        doc = await asyncio.to_thread(_imported, functools.partial(ws.import_file, folder, name, suffix, data=data))
        return await run(doc.tree_json)

    @app.post("/api/open-request")
    async def open_request(req: LocalFileRequest) -> dict[str, Any]:
        """`plainsolid open FILE`: tell every open tab to open a project file, or to offer the
        import of a STEP file from elsewhere; `delivered` says how many tabs heard it, so the
        caller raises a browser only when none did."""
        try:
            how = ws.locate(req.path)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"no such file: {exc}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        payload = {"event": "open-request", **how}
        for q in list(app.state.watchers):
            q.put_nowait(payload)
        return {**how, "delivered": len(app.state.watchers)}

    @app.post("/api/documents/new")
    async def new_document(req: NewRequest) -> dict[str, Any]:
        try:
            doc = ws.create(req.path, req.kind, req.name, req.material, req.of)
        except FileExistsError as exc:
            raise HTTPException(409, f"already exists: {exc}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return await run(doc.tree_json)

    @app.get("/api/documents/{doc_id}/tree")
    async def tree(doc_id: str) -> dict[str, Any]:
        return await run(_doc(doc_id).tree_json)

    @app.get("/api/documents/{doc_id}/source")
    def get_source(doc_id: str) -> dict[str, Any]:
        d = _doc(doc_id)
        with d.lock:
            return {"source": d.source, "hash": d.hash, "path": str(d.path)}

    @app.put("/api/documents/{doc_id}/source")
    async def put_source(doc_id: str, req: SourceRequest) -> dict[str, Any]:
        return await run(ws.set_source, _doc(doc_id), req.source, req.hash)

    @app.post("/api/documents/{doc_id}/edit")
    async def edit(doc_id: str, req: EditRequest) -> dict[str, Any]:
        body = req.model_dump(exclude={"hash"}, exclude_unset=True)
        base_hash = req.hash
        d = _doc(doc_id)
        return await run(ws.apply, d, body, base_hash)

    @app.post("/api/documents/{doc_id}/undo")
    async def undo(doc_id: str) -> dict[str, Any]:
        return await run(ws.undo, _doc(doc_id))

    @app.post("/api/documents/{doc_id}/redo")
    async def redo(doc_id: str) -> dict[str, Any]:
        return await run(ws.redo, _doc(doc_id))

    # --- geometry --------------------------------------------------------

    def _section(plane: str | None, offset: float, flip: bool) -> psection.SectionSpec | None:
        return psection.parse_spec(plane, offset, flip)

    @app.get("/api/documents/{doc_id}/mesh")
    async def mesh(doc_id: str, upto: str | None = None, tolerance: float | None = None,
                   plane: str | None = None, offset: float = 0.0, flip: bool = False,
                   overlay: str | None = None, rev: str | None = None) -> Response:
        d = _doc(doc_id)

        def work() -> Response:
            if overlay or rev:
                # the comparison against another document: three coloured items and the report
                report, items = ws.compare(d, overlay, rev, upto)
                m = pmesh.build(items, None, tolerance) if items else pmesh.empty()
                extra = {"hash": report["hash"], "revision": report["revision"], "upto": upto, "section": None, "compare": report}
                return Response(pmesh.encode(m, extra=extra), media_type="application/octet-stream")
            spec = _section(plane, offset, flip)
            m = d.mesh(upto, spec, tolerance)
            extra = {"hash": m["hash"], "revision": m["revision"], "upto": upto, "section": spec.to_json() if spec else None}
            return Response(pmesh.encode(m, extra=extra), media_type="application/octet-stream")
        return await run(work)

    @app.get("/api/documents/{doc_id}/query/{kind}")
    async def query(doc_id: str, kind: str, material: str | None = None, density: float | None = None,
                    upto: str | None = None) -> dict[str, Any]:
        d = _doc(doc_id)

        def work() -> dict[str, Any]:
            with d.lock:
                ev = d.evaluation_for(upto)
                kwargs: dict[str, Any] = {}
                if kind == "mass":
                    kwargs = {"material": material, "density": density}
                return {**pquery.run(ev, kind, **kwargs), "revision": d._revision}
        return await run(work)

    @app.post("/api/documents/{doc_id}/measure")
    async def measure(doc_id: str, req: MeasureRequest) -> dict[str, Any]:
        d = _doc(doc_id)

        def work() -> dict[str, Any]:
            items = d.geometry(req.upto, req.section.spec() if req.section else None)
            ev = d.evaluation_for(req.upto)
            a = prefs.measure_ref(ev, items, req.a)
            b = prefs.measure_ref(ev, items, req.b) if req.b is not None else None
            return pmeasure.measure(items, a, b)
        return await run(work)

    @app.post("/api/documents/{doc_id}/locate")
    async def locate(doc_id: str, req: LocateRequest) -> dict[str, Any]:
        """The mesh ids a selector or a feature name picks, so the browser can light what a
        reference in the panel refers to. A reference that resolves to nothing (a fillet's edge
        that the fillet consumed) answers empty lists with the reason, not an error: hovering
        must never fail."""
        d = _doc(doc_id)

        def work() -> dict[str, Any]:
            try:
                return prefs.highlight_ids(d.evaluation_for(req.upto), d.geometry(req.upto, None), req.refs)
            except SelectorError as exc:
                return {"faces": [], "edges": [], "vertices": [], "error": str(exc)}
        return await run(work)

    @app.post("/api/documents/{doc_id}/render")
    async def render(doc_id: str, req: RenderRequest) -> Response:
        d = _doc(doc_id)

        def work() -> Response:
            ids: dict[str, list[int]] = {"faces": [], "edges": [], "vertices": []}
            if req.overlay or req.rev:
                _, items = ws.compare(d, req.overlay, req.rev, req.upto)
                m = pmesh.build(items, None) if items else pmesh.empty()
            else:
                spec = req.section.spec() if req.section else None
                m = d.mesh(req.upto, spec)
                if req.highlight:
                    ids = prefs.highlight_ids(d.evaluation_for(req.upto), d.geometry(req.upto, spec), req.highlight)
            if not m["face_ranges"]:
                raise HTTPException(400, "the document has no geometry to render")
            with _render_lock:
                png = prender.render_png(m, req.view, (req.width, req.height), ids["faces"] or None, edges=req.edges,
                                         highlight_edges=ids["edges"] or None, highlight_vertices=ids["vertices"] or None)
            return Response(png, media_type="image/png")
        return await run(work)

    @app.post("/api/documents/{doc_id}/compare")
    async def compare(doc_id: str, req: CompareRequest) -> dict[str, Any]:
        """What this document's geometry adds to and removes from another's (or from
        itself at a git revision), by volume and region."""
        d = _doc(doc_id)
        report, _ = await run(ws.compare, d, req.other, req.rev, req.upto)
        return report

    @app.post("/api/documents/{doc_id}/solve")
    async def solve(doc_id: str, req: SolveRequest) -> dict[str, Any]:
        """Preview a sketch solution, optionally with a drag, without writing the file."""
        d = _doc(doc_id)
        try:
            return (await run(ws.solve, d, req.sketch, req.drag)).to_json()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/documents/{doc_id}/preview")
    async def preview(doc_id: str, req: PreviewRequest) -> dict[str, Any]:
        """Evaluate the document with an edit applied, writing nothing: poses and verdicts for a live preview."""
        d = _doc(doc_id)
        return await run(ws.preview, d, req.op.model_dump(exclude_unset=True), req.choose_flip)

    @app.post("/api/documents/{doc_id}/preview-mesh")
    async def preview_mesh(doc_id: str, req: PreviewRequest) -> Response:
        """The mesh of the document with an edit applied, nothing written: a feature dialog's live preview."""
        d = _doc(doc_id)
        try:
            m, extra = await run(ws.preview_mesh, d, req.op.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return Response(pmesh.encode(m, extra=extra), media_type="application/octet-stream")

    @app.post("/api/documents/{doc_id}/drag")
    async def drag(doc_id: str, req: DragRequest) -> dict[str, Any]:
        """One step of dragging an instance along the motions its mates leave free; nothing is written."""
        d = _doc(doc_id)
        try:
            return await run(ws.drag, d, req.instance, req.poses, req.translate, req.rotate)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/documents/{doc_id}/views")
    def get_views(doc_id: str) -> dict[str, Any]:
        return pviews.load(_doc(doc_id).path)

    @app.put("/api/documents/{doc_id}/views")
    def put_views(doc_id: str, req: ViewsRequest) -> dict[str, Any]:
        return pviews.save(_doc(doc_id).path, req.views)

    @app.post("/api/documents/{doc_id}/export")
    async def export(doc_id: str, req: ExportRequest) -> dict[str, Any]:
        d = _doc(doc_id)

        def work() -> dict[str, Any]:
            ev = d.ensure_evaluated()
            target = Path(req.path)
            if not target.is_absolute():
                target = d.path.parent / target
            target.parent.mkdir(parents=True, exist_ok=True)
            pio.export(ev, req.format, target)
            return {"path": str(target), "format": req.format}
        return await run(work)

    # --- events ----------------------------------------------------------

    @app.websocket("/api/documents/{doc_id}/events")
    async def events(websocket: WebSocket, doc_id: str) -> None:
        try:
            ws.get(doc_id)
        except KeyError:
            await websocket.close(code=4004)
            return
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue()
        app.state.subscribers.setdefault(doc_id, set()).add(q)
        try:
            d = ws.get(doc_id)
            # Do not block the event loop if a kernel operation holds the document lock.
            def hello():
                with d.lock:
                    return {"event": "hello", "doc": doc_id, "hash": d.hash, "revision": d.revision}
            await websocket.send_json(await asyncio.to_thread(hello))
            while True:
                payload = await q.get()
                await websocket.send_json(payload)
        except WebSocketDisconnect:
            pass
        finally:
            app.state.subscribers.get(doc_id, set()).discard(q)

    @app.websocket("/api/events")
    async def workspace_events(websocket: WebSocket) -> None:
        """One per tab: what `plainsolid open` asks for, and the tab count the idle exit watches."""
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue()
        app.state.watchers.add(q)
        try:
            await websocket.send_json({"event": "hello", "root": str(ws.root)})
            await _pump(websocket, q)
        except WebSocketDisconnect:
            pass
        finally:
            app.state.watchers.discard(q)
            app.state.last_activity = time.monotonic()

    # --- client ----------------------------------------------------------

    dist = Path(__file__).parent / "static"
    if serve_client and dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="client")

    return app


async def _pump(websocket: WebSocket, q: asyncio.Queue) -> None:
    """Send queued payloads until the client goes away, noticing the disconnect at once
    rather than at the next send."""
    recv = asyncio.ensure_future(websocket.receive())
    try:
        while True:
            get = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait({recv, get}, return_when=asyncio.FIRST_COMPLETED)
            if recv in done:
                get.cancel()
                if recv.result().get("type") == "websocket.disconnect":
                    return
                recv = asyncio.ensure_future(websocket.receive())
                continue
            await websocket.send_json(get.result())
    finally:
        recv.cancel()


async def _idle(app: FastAPI) -> None:
    """Stop a server nobody uses: no tab connected and no request for idle_minutes. Off
    unless serve() was given a limit, which `plainsolid open` does for the servers it starts."""
    try:
        while True:
            await asyncio.sleep(15)
            server, minutes = app.state.server, app.state.idle_minutes
            if server is not None and minutes > 0 and not app.state.watchers \
                    and time.monotonic() - app.state.last_activity > minutes * 60:
                server.should_exit = True
    except asyncio.CancelledError:
        return


async def _watch(app: FastAPI, ws: Workspace) -> None:
    """Push external edits of open documents to clients."""
    try:
        from watchfiles import awatch
    except ImportError:
        return
    try:
        async for changes in awatch(ws.root, recursive=True):
            for _change, path in changes:
                doc = ws.path_for(path)
                if doc is not None:
                    await asyncio.to_thread(ws.reload_from_disk, doc)
                for dep in ws.dependents_of(path):
                    await asyncio.to_thread(ws.invalidate, dep)
    except asyncio.CancelledError:
        return


def serve(root: str | Path | None = None, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          open_paths: list[str] | None = None, idle_minutes: float = 0.0) -> None:
    import uvicorn

    app = create_app(root)
    app.state.port = port
    app.state.idle_minutes = idle_minutes
    for p in open_paths or []:
        app.state.workspace.open(p)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    app.state.server = server  # so /api/shutdown and the idle exit can stop it
    server.run()
