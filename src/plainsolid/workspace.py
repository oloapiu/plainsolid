"""Open documents: load, evaluate, edit with a stale-hash check, undo, reload.

Used by the server and the CLI alike. The file on disk is the truth; every
successful edit writes it immediately.
"""
from __future__ import annotations

import keyword
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from . import assembly as pasm
from . import dependencies
from . import edit as edit_ops
from . import section as psection
from .evaluate import Evaluation, Item, evaluate
from .literals import python_literal
from .model import Document, source_hash
from .operations import validate_operation
from .parse import parse_document
from .stepimport import ImportError_, import_step_tree, relocated, select_node, split_fragment

STEP_SUFFIXES = (".step", ".stp")
CACHE_DIR = ".plainsolid-cache"
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", CACHE_DIR, ".pytest_cache", ".ruff_cache", "dist", "build"}


def _author() -> str:
    """The git user name, if git is configured; otherwise empty."""
    import subprocess

    try:
        out = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=2, check=False)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def template_source(name: str, kind: str = "part", material: str = "al6061", author: str | None = None,
                    of: str | None = None) -> str:
    """A fresh model file: metadata only, ready for a first sketch. A drawing names
    the model it shows (`of`) and starts with four views placed for an A4 sheet."""
    if kind not in ("part", "assembly", "drawing"):
        raise ValueError(f"kind must be part, assembly or drawing, got {kind!r}")
    author = _author() if author is None else author
    fields = [f"name={python_literal(name)}"]
    if kind == "assembly":
        fields.insert(0, 'kind="assembly"')
    elif kind == "drawing":
        if not of:
            raise ValueError("a drawing needs the model it shows: of=\"part.py\"")
        fields.insert(0, 'kind="drawing"')
        fields.append(f"of={python_literal(of)}")
        fields.append('sheet="A4"')
    else:
        fields.append(f"material={python_literal(material)}")
    fields.append('revision="A"')
    if author:
        fields.append(f"author={python_literal(author)}")
    src = "from plainsolid import *\n\n" + f"meta({', '.join(fields)})\n"
    if kind == "drawing":
        src += (
            "\n"
            'front = view("front", direction=FRONT, at=(70, 135))\n'
            'top = view("top", direction=TOP, at=(70, 60))\n'
            'right = view("right", direction=RIGHT, at=(160, 135))\n'
            'iso = view("iso", direction=ISO, at=(235, 135), hidden=False)\n'
        )
    return src


def identifier(text: str, taken: set[str] | None = None) -> str:
    """A Python identifier made from a name, unique against `taken`."""
    ident = re.sub(r"_+", "_", re.sub(r"\W", "_", str(text))).strip("_") or "part"
    if ident[0].isdigit():
        ident = "p_" + ident
    if keyword.iskeyword(ident) or ident in ("meta", "param", "sketch", "instance", "fixed", "body"):
        ident += "_"
    if taken:
        base, i = ident, 1
        while ident in taken:
            i += 1
            ident = f"{base}_{i}"
    return ident


def wrapper_source(step_name: str, kind: str = "assembly", node: str | None = None) -> str:
    """The model file that opens a foreign STEP file, or one node of it: a review
    assembly, or, for one solid, a part whose body is that solid."""
    label = node.rsplit(".", 1)[-1] if node else Path(step_name).stem
    ident = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in label)
    if not ident or ident[0].isdigit():
        ident = "step_" + ident
    if keyword.iskeyword(ident) or not ident.isidentifier():
        ident = identifier(ident)
    target = step_name + (f"#{node}" if node else "")
    if kind == "part":
        return (
            "from plainsolid import *\n\n"
            f'meta(name={python_literal(label)})\n\n'
            f'body = import_step("body", {python_literal(target)})\n'
        )
    return (
        "from plainsolid import *\n\n"
        f'meta(kind="assembly", name={python_literal(label)})\n\n'
        f'{ident} = import_step({python_literal(ident)}, {python_literal(target)})\n'
    )


def step_kind(path: Path, node: str | None = None) -> str:
    """What a STEP file, or one node of it, opens as: a part when it holds exactly
    one solid, else an assembly."""
    try:
        tree = import_step_tree(path)
        if node:
            tree = select_node(tree, node)
    except ImportError_:
        return "assembly"
    solids = sum(len(leaf.shape.solids()) for leaf in tree.leaves() if leaf.shape is not None)
    return "part" if solids == 1 else "assembly"


def _has_mates(source: str) -> bool:
    """Without a mate nothing moves, so there is no pose to write back."""
    return any(f"\n{kind}(" in source or source.startswith(f"{kind}(") for kind in
               ("fixed", "coincident", "concentric", "distance", "parallel", "angle"))


def _preview_rank(o: dict[str, Any]) -> tuple[int, int]:
    return (0 if o["ok"] else 1, len(o.get("conflicting", [])))


class StaleHashError(ValueError):
    def __init__(self, expected: str, got: str | None):
        super().__init__(f"document changed: expected hash {expected}, got {got}")
        self.expected, self.got = expected, got


@dataclass
class OpenDocument:
    path: Path
    source: str
    document: Document
    evaluation: Evaluation | None = None
    undo: list[str] = field(default_factory=list)
    redo: list[str] = field(default_factory=list)
    lock: Any = field(default_factory=threading.RLock)
    cache_dir: Path | None = None
    _geometry: dict[str, list[Item]] = field(default_factory=dict)
    _meshes: dict[str, dict] = field(default_factory=dict)
    _cache: Evaluation | None = None  # the last full evaluation; unchanged feature prefixes are reused
    _revision: str = ""

    @property
    def revision(self) -> str:
        return dependencies.revision(self.document)

    @property
    def id(self) -> str:
        return source_hash(str(self.path))[:8]

    @property
    def hash(self) -> str:
        return source_hash(self.source)

    def ensure_evaluated(self) -> Evaluation:
        # Edits share this lock: an old evaluation cannot overwrite their invalidation.
        # Dependencies can still change on disk, so verify their revision before publishing.
        with self.lock:
            while True:
                revision = self.revision
                if self.evaluation is not None and self._revision == revision:
                    return self.evaluation
                ev = evaluate(self.document, cache=self._cache)
                if revision != self.revision:
                    continue
                self.evaluation = self._cache = ev
                self._revision = revision
                self._geometry.clear()
                self._meshes.clear()
                return ev

    def evaluation_for(self, upto: str | None) -> Evaluation:
        full = self.ensure_evaluated()
        return evaluate(full.document, upto=upto, cache=full) if upto else full

    def mesh(self, upto: str | None = None, section: psection.SectionSpec | None = None,
             tolerance: float | None = None) -> dict:
        """The built mesh for these parameters, cached in memory so repeated
        section requests (a slider being fiddled with) cost nothing."""
        with self.lock:
            from . import mesh as pmesh

            self.ensure_evaluated()
            key = f"{self._revision}|{upto}|{section.key if section else ''}|{tolerance}"
            m = self._meshes.get(key)
            if m is None:
                items = self.geometry(upto, section)
                m = pmesh.build(items, self.cache_dir, tolerance) if items else pmesh.empty()
                m = {**m, "hash": self.hash, "revision": self._revision}
                if len(self._meshes) > 16:
                    self._meshes.pop(next(iter(self._meshes)))
                self._meshes[key] = m
            return m

    def geometry(self, upto: str | None = None, section: psection.SectionSpec | None = None) -> list[Item]:
        """Items for the mesh and measurements, cached per (hash, upto, section)."""
        with self.lock:
            self.ensure_evaluated()
            key = f"{self._revision}|{upto}|{section.key if section else ''}"
            items = self._geometry.get(key)
            if items is None:
                items = self.evaluation_for(upto).items()
                if section is not None:
                    items = psection.apply(items, section)
                if len(self._geometry) > 8:
                    self._geometry.pop(next(iter(self._geometry)))
                self._geometry[key] = items
            return items

    def names(self) -> dict[str, list[str]]:
        """Names an expression may use: parameters and sketch dimensions (sketch.dim)."""
        dims = []
        for f in self.document.features:
            if f.kind == "sketch" and f.variable:
                dims.extend(f"{f.variable}.{c.name}" for c in f.constraints if c.is_dimension)
        return {"params": [p.name for p in self.document.params], "dimensions": dims}

    def tree_json(self) -> dict[str, Any]:
        with self.lock:
            ev = self.ensure_evaluated()
            out = self.document.to_json()
            out["id"] = self.id
            out["revision"] = self._revision
            out["names"] = self.names()
            out["evaluation"] = ev.to_json()
            try:
                deps = edit_ops.dependents_all(self.source)
            except Exception:  # noqa: BLE001 - a file that does not parse has no cascade to show
                deps = {}
            for f in out["features"]:
                f["result"] = ev.result_json(f["name"])
                dep = deps.get(f["name"])
                f["dependents"] = {"features": dep["features"], "entities": len(dep["entities"])} if dep else {"features": [], "entities": 0}
            return out


class Workspace:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or ".").resolve()
        self.docs: dict[str, OpenDocument] = {}
        self.listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self.max_undo = 200
        self._compares: dict[str, tuple[dict[str, Any], list[Item]]] = {}

    # --- documents ---------------------------------------------------------

    def _inside(self, path: str | Path) -> Path:
        """`path` resolved against the project root; refused outside it, so wrappers,
        sidecars and new documents can never land elsewhere by mistake."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        if not p.is_relative_to(self.root):
            raise ValueError(f"{p} is outside the project {self.root}: copy it into the project first")
        return p

    def open(self, path: str | Path) -> OpenDocument:
        """A model file, a STEP file (through its wrapper), or `x.step#node`: one
        sub-assembly of the file as a document of its own. Inside the project only."""
        text, node = split_fragment(str(path))
        p = self._inside(text)
        if p.suffix.lower() in STEP_SUFFIXES:
            if not p.exists():
                raise FileNotFoundError(p)
            p = self._wrapper(p, node)
        elif node:
            raise ValueError(f"only a STEP file takes a #node, not {p.name}")
        for d in self.docs.values():
            if d.path == p:
                return d
        source = p.read_text(encoding="utf-8")
        doc = OpenDocument(path=p, source=source, document=parse_document(source, str(p)),
                           cache_dir=self.root / CACHE_DIR)
        self.docs[doc.id] = doc
        return doc

    def _wrapper(self, step: Path, node: str | None) -> Path:
        """The model file that opens a STEP file or one node of it, written on the
        first open: `x.py` next to `x.step`; `x.sub.part.py` for the node `root.sub.part`,
        which is shown in its own coordinates. A short fragment is written out in full."""
        below = ""
        if node:
            try:
                node = select_node(import_step_tree(step), node).path
            except ImportError_ as exc:
                raise ValueError(str(exc)) from None
            below = node.split(".", 1)[1] if "." in node else ""
            if not below:
                node = None  # the root is the whole file
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in below)
        wrapper = step.with_name(f"{step.stem}.{safe}.py") if safe else step.with_suffix(".py")
        if not wrapper.exists():
            wrapper.write_text(wrapper_source(step.name, step_kind(step, node), node), encoding="utf-8")
        return wrapper

    def locate(self, path: str | Path) -> dict[str, Any]:
        """How a local file would open here: `{"action": "open", "path": rel}` for a file
        inside the project, `{"action": "import", "source": abs, "name": stem, "suffix": ...}`
        for a STEP file elsewhere, which has to be copied in first."""
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        if p.is_relative_to(self.root):
            return {"action": "open", "path": p.relative_to(self.root).as_posix()}
        if p.suffix.lower() in STEP_SUFFIXES:
            return {"action": "import", "source": str(p), "name": p.stem, "suffix": p.suffix}
        raise ValueError(f"{p} is outside the project, and only STEP files are imported")

    def import_file(self, folder: str, name: str, suffix: str, *, source: str | Path | None = None,
                    data: bytes | None = None) -> OpenDocument:
        """Copy a STEP file into the project, from a local path or from bytes, as
        `folder/name.suffix`, then open it. The original is never touched. Refuses to
        overwrite, and anything but STEP."""
        if suffix.lower() not in STEP_SUFFIXES:
            raise ValueError(f"only STEP files are imported, not {suffix or 'a file without a suffix'}")
        stem = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name.strip()).strip("._")
        if not stem:
            raise ValueError("the name is empty")
        target = self._inside(Path(folder or ".") / f"{stem}{suffix}")
        if target.exists():
            raise FileExistsError(target)
        if source is not None:
            source = Path(source).expanduser().resolve()
            if not source.is_file():
                raise FileNotFoundError(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        if source is not None:
            shutil.copyfile(source, tmp)
        else:
            tmp.write_bytes(data or b"")
        os.replace(tmp, target)
        return self.open(target)

    def create(self, path: str | Path, kind: str = "part", name: str | None = None,
               material: str = "al6061", of: str | None = None) -> OpenDocument:
        """Write a new model file from the template and open it, inside the project
        only. Refuses to overwrite."""
        p = self._inside(path)
        if p.suffix != ".py":
            p = p.with_suffix(".py")
        if p.exists():
            raise FileExistsError(p)
        stem = name or p.stem
        p.parent.mkdir(parents=True, exist_ok=True)
        if of:
            rel = Path(of)
            if rel.is_absolute():
                try:
                    rel = rel.relative_to(p.parent)
                except ValueError:
                    pass
            of = rel.as_posix()
        p.write_text(template_source(stem, kind, material, of=of), encoding="utf-8")
        return self.open(p)

    def get(self, doc_id: str) -> OpenDocument:
        try:
            return self.docs[doc_id]
        except KeyError:
            raise KeyError(f"no open document {doc_id!r}") from None

    def close(self, doc_id: str) -> None:
        self.docs.pop(doc_id, None)

    def files(self) -> list[dict[str, str]]:
        """Model files and STEP files under the project, relative to its root."""
        out: list[dict[str, str]] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                p = Path(dirpath) / name
                rel = p.relative_to(self.root).as_posix()
                suffix = p.suffix.lower()
                if suffix in STEP_SUFFIXES:
                    wrapper = p.with_suffix(".py")
                    entry: dict[str, str] = {"path": rel, "kind": "step"}
                    if wrapper.exists():
                        entry["wrapper"] = wrapper.relative_to(self.root).as_posix()  # opening the STEP opens this
                    out.append(entry)
                elif suffix == ".py":
                    try:
                        head = p.read_text(encoding="utf-8", errors="replace")[:4000]
                    except OSError:
                        continue
                    if "from plainsolid import" not in head:
                        continue
                    kind = "part"
                    for k in ("assembly", "drawing"):
                        if f'kind="{k}"' in head or f"kind='{k}'" in head:
                            kind = k
                    out.append({"path": rel, "kind": kind})
        return out

    def path_for(self, path: str | Path) -> OpenDocument | None:
        p = Path(path).resolve()
        for d in self.docs.values():
            if d.path == p:
                return d
        return None

    def dependents_of(self, path: str | Path) -> list[OpenDocument]:
        """Open assemblies that instance the file at `path`."""
        p = str(Path(path).resolve())
        return [d for d in list(self.docs.values())
                if any(stamp[0] == p for stamp in dependencies.file_dependencies(dependencies.references(d.document)))]

    def invalidate(self, doc: OpenDocument) -> None:
        """A file the document depends on changed: re-evaluate and tell the clients."""
        with doc.lock:
            doc.evaluation = None
            doc._geometry.clear()
            doc._meshes.clear()
        self._emit(doc, "dependency")

    # --- changes ---------------------------------------------------------

    def _emit(self, doc: OpenDocument, event: str, **data: Any) -> None:
        payload = {"event": event, "doc": doc.id, "hash": doc.hash, "revision": doc.revision, **data}
        for fn in list(self.listeners):
            fn(doc.id, payload)

    def _commit(self, doc: OpenDocument, new_source: str, *, write: bool, push_undo: bool, event: str) -> None:
        parsed = parse_document(new_source, str(doc.path))
        if write:
            self._write_atomic(doc, new_source)
        if push_undo:
            doc.undo.append(doc.source)
            del doc.undo[:-self.max_undo]
            doc.redo.clear()
        doc.source = new_source
        doc.document = parsed
        doc.evaluation = None
        doc._geometry.clear()
        doc._meshes.clear()
        self._emit(doc, event)
        # assemblies that instance this file show the change at once, before the watcher notices
        for dep in self.dependents_of(doc.path):
            if dep is not doc:
                self.invalidate(dep)

    def _check_disk(self, doc: OpenDocument) -> None:
        """The watcher is a notification mechanism, not a prerequisite for conflict checks."""
        source = doc.path.read_text(encoding="utf-8")
        if source != doc.source:
            old_hash = doc.hash
            self._commit(doc, source, write=False, push_undo=True, event="external")
            raise StaleHashError(doc.hash, old_hash)

    def _write_atomic(self, doc: OpenDocument, source: str) -> None:
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=doc.path.parent,
                                             prefix=f".{doc.path.name}.", suffix=".tmp", delete=False) as fh:
                path = Path(fh.name)
                os.chmod(path, doc.path.stat().st_mode & 0o777)
                fh.write(source)
                fh.flush()
                os.fsync(fh.fileno())
            # Editing/solving can take time; check again immediately before replacing the file.
            self._check_disk(doc)
            os.replace(path, doc.path)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    SKETCH_OPS: ClassVar[frozenset[str]] = frozenset({
        "add_sketch_entity", "delete_sketch_entity", "set_entity_argument", "add_constraint",
        "delete_constraint", "set_constraint_value", "solve_sketch", "batch",
    })

    def apply(self, doc: OpenDocument, op: dict[str, Any], base_hash: str | None) -> dict[str, Any]:
        op = validate_operation(op)
        with doc.lock:
            self._check_disk(doc)
            if base_hash is not None and base_hash != doc.hash:
                raise StaleHashError(doc.hash, base_hash)
            old = doc.source
            kind = op.get("op")
            extra: dict[str, Any] = {}
            if kind == "solve_sketch":
                new = old
            elif kind == "explode_import":
                new, mapping = self._explode(doc, old, str(op["feature"]))
                extra["instances"] = mapping
                extra["skipped"] = list(self._skipped)
            elif kind == "make_editable":
                new, extra = self._make_editable(doc, old, op)
            else:
                new = edit_ops.apply(old, op)
            solution = None
            sketches = self._batch_sketches(op) if kind == "batch" else \
                [op["sketch"]] if kind in self.SKETCH_OPS and op.get("sketch") else []
            for sketch_name in sketches:
                new, solution = self._write_back(doc, new, sketch_name, None if kind == "batch" else op.get("drag"))
            if not sketches and kind != "replace_source" and doc.document.kind == "assembly" and _has_mates(new):
                new = self._write_back_poses(doc, new)
            if new == old:
                return {"changed": False, "hash": doc.hash, "diff": "",
                        "solution": solution.to_json() if solution else None, **extra}
            self._commit(doc, new, write=True, push_undo=True, event="changed")
            return {"changed": True, "hash": doc.hash, "diff": edit_ops.unified_diff(old, new, doc.path.name),
                    "solution": solution.to_json() if solution else None, **extra}

    # --- turning imported STEP geometry into parts ---------------------------------

    def _explode(self, doc: OpenDocument, source: str, feature_name: str) -> tuple[str, dict[str, str]]:
        """Replace a review import by one instance per body of the file, each pointing
        into the same STEP file by its path and posed where the file put it. Returns
        the new source and the map from node path to instance name."""
        parsed = parse_document(source, str(doc.path))
        feature = parsed.feature(feature_name)
        if feature is None or feature.kind != "import_step":
            raise edit_ops.EditError(f"{feature_name!r} is not an import_step feature")
        if parsed.kind != "assembly":
            raise edit_ops.EditError("only an import in an assembly document can be turned into instances")
        file, fragment = split_fragment(feature.args["path"])
        path = (doc.path.parent / file).resolve()
        try:
            tree = import_step_tree(path)
            node = relocated(select_node(tree, fragment)) if fragment else tree  # posed as the viewer showed them
        except ImportError_ as exc:
            raise edit_ops.EditError(str(exc)) from None
        taken = {f.name for f in parsed.features} | {p.name for p in parsed.params}
        mapping: dict[str, str] = {}
        statements: list[str] = []
        self._skipped = [leaf.path for leaf in node.leaves() if leaf.shape is None or not leaf.shape.solids()]
        for leaf in node.leaves():
            if leaf.shape is None or not leaf.shape.solids():
                continue  # a sheet or a wire in the file: nothing an instance could hold
            name = identifier(leaf.name, taken)
            taken.add(name)
            m = leaf.transform or [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
            pose = pasm.Pose(np.array([row[:3] for row in m[:3]], float), np.array([row[3] for row in m[:3]], float))
            args = {"path": f"{file}#{leaf.path}", "at": list(pose.at()), "rotate": list(pose.rotate())}
            statements.append(edit_ops.compose_feature("instance", name, args))
            # the tree the client shows names the root after the feature, the file after itself
            mapping[leaf.path] = name
            mapping[feature_name + leaf.path[len(node.name):]] = name
        if not statements:
            raise edit_ops.EditError(f"{path.name} holds no bodies to instance")
        new = edit_ops.add_features(source, feature_name, statements)
        new = edit_ops.delete_feature(new, feature_name, cascade=False)
        return new, mapping

    def _make_editable(self, doc: OpenDocument, source: str, op: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Give a vendor body a part file of its own: a part wrapping the STEP file (or
        one node of it), every instance of that product repointed at the part. A node
        of a review import first turns the import into instances."""
        new = source
        extra: dict[str, Any] = {}
        name = op.get("instance")
        if op.get("leaf"):
            parsed = parse_document(source, str(doc.path))
            leaf = str(op["leaf"])
            owner = next((f for f in parsed.features if f.kind == "import_step" and (leaf == f.name or leaf.startswith(f.name + "."))), None)
            if owner is None:
                raise edit_ops.EditError(f"no imported node {leaf!r}")
            new, mapping = self._explode(doc, new, owner.name)
            extra["instances"] = mapping
            extra["skipped"] = list(self._skipped)
            name = mapping.get(leaf)
            if name is None:
                raise edit_ops.EditError(f"{leaf!r} is not a body of {owner.name!r}; pick one of its parts")
        parsed = parse_document(new, str(doc.path))
        inst = parsed.feature(str(name))
        if inst is None or inst.kind != "instance":
            raise edit_ops.EditError(f"{name!r} is not an instance")
        file, fragment = split_fragment(inst.args["path"])
        path = (doc.path.parent / file).resolve()
        if path.suffix.lower() not in STEP_SUFFIXES:
            raise edit_ops.EditError(f"{inst.name!r} is already a part file ({file}); open it to edit it")
        try:
            rec = pasm.load_part(path, fragment)
        except pasm.AssemblyError as exc:
            raise edit_ops.EditError(str(exc)) from None
        n_solids = len(rec.shape.solids())
        if n_solids != 1:
            raise edit_ops.EditError(f"{inst.name!r} holds {n_solids} solids; a part is one solid body")
        product = rec.name
        # one part file per product, named after the body that was picked
        part_path = self._part_file_for(doc, inst.name, file, fragment)
        if not part_path.exists():
            rel = os.path.relpath(path, part_path.parent).replace(os.sep, "/") + (f"#{fragment}" if fragment else "")
            material = inst.args.get("material")
            fields = [f"name={python_literal(part_path.stem)}"] + ([f"material={python_literal(material)}"] if material else [])
            part_path.write_text(
                "from plainsolid import *\n\n"
                f"meta({', '.join(fields)})\n\n"
                f'body = import_step("body", {python_literal(rel)})\n', encoding="utf-8")
        target = os.path.relpath(part_path, doc.path.parent).replace(os.sep, "/")
        repointed = []
        tree = import_step_tree(path)
        for f in parsed.features:
            if f.kind != "instance":
                continue
            f_file, f_frag = split_fragment(f.args["path"])
            if (doc.path.parent / f_file).resolve() != path:
                continue
            if f_frag != fragment:
                try:
                    other = select_node(tree, f_frag) if f_frag else tree
                except ImportError_:
                    continue
                if (other.product or other.name) != product:
                    continue
            new = edit_ops.set_argument(new, f.name, "path", python_literal(target))
            repointed.append(f.name)
        extra.update({"part": str(part_path), "instance": str(name), "repointed": repointed})
        return new, extra

    def _part_file_for(self, doc: OpenDocument, inst_name: str, file: str, fragment: str | None) -> Path:
        """The part file that wraps a STEP node: an existing wrapper of the same node, else a new name."""
        target = file + (f"#{fragment}" if fragment else "")
        wanted = f'import_step("body", {python_literal(target)})'
        for p in sorted(doc.path.parent.glob("*.py")):
            try:
                if wanted in p.read_text(encoding="utf-8"):
                    return p
            except OSError:
                continue
        base = identifier(inst_name)
        candidate = doc.path.parent / f"{base}.py"
        i = 1
        while candidate.exists():
            i += 1
            candidate = doc.path.parent / f"{base}_{i}.py"
        return candidate

    def _batch_sketches(self, op: dict[str, Any]) -> list[str]:
        """The sketches a batch touches, in order, each once: every one is solved and written back."""
        out: list[str] = []
        for sub in op.get("ops", []):
            if sub.get("op") == "batch":
                names = self._batch_sketches(sub)
            elif sub.get("op") in self.SKETCH_OPS and sub.get("sketch"):
                names = [str(sub["sketch"])]
            else:
                names = []
            out.extend(n for n in names if n not in out)
        return out

    def _write_back(self, doc: OpenDocument, source: str, sketch_name: str, drag: dict | None):
        """Solve the named sketch of `source` (with an optional drag) and write the
        solved coordinates back into it. Sketch solve failures leave the text as is."""
        from .evaluate import sketch_context, solve_feature_sketch

        parsed = parse_document(source, str(doc.path))
        feature = parsed.feature(sketch_name)
        if feature is None or feature.kind != "sketch" or parsed.errors:
            return source, None
        try:
            body, plane, identity = sketch_context(parsed, feature, cache=doc._cache)
            targets = {k: (float(v[0]), float(v[1])) for k, v in (drag or {}).items()}
            solution = solve_feature_sketch(feature, body, targets or None, plane, identity)
        except ValueError:
            return source, None
        return edit_ops.write_back(source, sketch_name, solution.coords), solution

    def _write_back_poses(self, doc: OpenDocument, source: str) -> str:
        """Solve the assembly of `source` and write the poses into its instance statements."""
        parsed = parse_document(source, str(doc.path))
        if parsed.kind != "assembly" or parsed.errors:
            return source
        ev = evaluate(parsed, cache=doc._cache)
        if ev.solution is None or ev.solution.conflicting:
            return source  # an unsatisfied set of mates is not a pose worth keeping
        return edit_ops.write_poses(source, pasm.file_poses(ev.solution, ev.assembly))

    FLIPPABLE = ("coincident", "distance", "angle")

    def preview(self, doc: OpenDocument, op: dict[str, Any], choose_flip: bool = False) -> dict[str, Any]:
        """Evaluate the document with an edit applied, writing nothing: the poses, the
        solver's verdict and the results of the features the edit adds. With `choose_flip`
        a new mate is tried both ways and the orientation that turns the parts less from
        where they are wins; the answer says which `flip` that is."""
        with doc.lock:
            base = doc.ensure_evaluated()
            candidates = [op]
            if choose_flip and op.get("op") == "add_feature" and op.get("kind") in self.FLIPPABLE:
                flipped = {**op, "args": {**op.get("args", {}), "flip": not op.get("args", {}).get("flip", False)}}
                candidates = [op, flipped]
            best: dict[str, Any] | None = None
            for cand in candidates:
                try:
                    source = edit_ops.apply(doc.source, cand)
                except edit_ops.EditError as exc:
                    return {"ok": False, "error": str(exc), "hash": doc.hash}
                parsed = parse_document(source, str(doc.path))
                ev = evaluate(parsed, cache=doc._cache)
                before = {f.name for f in doc.document.features}
                results = {r.name: r.to_json() for r in ev.results if r.name not in before}
                sol = ev.solution
                out: dict[str, Any] = {
                    "ok": not parsed.errors and all(r["ok"] for r in results.values()),
                    "hash": doc.hash, "errors": [e.to_json() for e in parsed.errors], "results": results,
                    "poses": sol.to_json()["poses"] if sol is not None else {},
                    "assembly": sol.to_json() if sol is not None else None,
                    "flip": bool(cand.get("args", {}).get("flip", False)),
                    "conflicting": list(sol.conflicting) if sol is not None else [],
                    "rotation": pasm.rotation_from(base.solution, sol.to_json()["poses"]) if sol is not None and base.solution is not None else 0.0,
                }
                # a satisfiable orientation beats a conflicting one; then the smaller turn; a tie keeps the convention
                if best is None or _preview_rank(out) < _preview_rank(best) or (
                        _preview_rank(out) == _preview_rank(best) and out["rotation"] < best["rotation"] - 1e-6):
                    best = out
            return best or {"ok": False, "error": "nothing to preview", "hash": doc.hash}

    def preview_mesh(self, doc: OpenDocument, op: dict[str, Any], tolerance: float | None = None) -> tuple[dict, dict[str, Any]]:
        """The mesh of the document with an edit applied, writing nothing, and what the
        edit's new features made of it (ok, an error message, faces created)."""
        with doc.lock:
            from . import mesh as pmesh

            try:
                source = edit_ops.apply(doc.source, op)
            except edit_ops.EditError as exc:
                raise ValueError(str(exc)) from None
            parsed = parse_document(source, str(doc.path))
            ev = evaluate(parsed, cache=doc._cache)
            before = {f.name for f in doc.document.features}
            new = [r for r in ev.results if r.name not in before]
            error = next((r.error.message for r in new if r.error), None) or (parsed.errors[0].message if parsed.errors else None)
            items = ev.items()
            m = pmesh.build(items, doc.cache_dir, tolerance) if items else pmesh.empty()
            return m, {"ok": error is None, "error": error, "hash": doc.hash, "faces": sum(r.faces_created for r in new),
                       "seconds": round(ev.seconds, 4)}

    def drag(self, doc: OpenDocument, instance: str, poses: dict[str, Any] | None,
             translate: list[float] | None, rotate: list[float] | None) -> dict[str, Any]:
        """One step of dragging an instance along the motions its mates leave free."""
        with doc.lock:
            ev = doc.ensure_evaluated()
            if ev.document.kind != "assembly" or ev.solution is None:
                raise ValueError("dragging needs an assembly with instances")
            try:
                return pasm.drag(ev.assembly, ev.solution, instance, poses, translate, rotate)
            except pasm.AssemblyError as exc:
                raise ValueError(str(exc)) from None

    def compare(self, doc: OpenDocument, other: str | None = None, rev: str | None = None,
                upto: str | None = None) -> tuple[dict[str, Any], list[Item]]:
        """The volumetric change report and the overlay items (common, added, removed)
        against another file or this file at a git revision; cached per document hash."""
        with doc.lock:
            from . import compare as pcompare

            base = doc.evaluation_for(upto)
            other_doc = pcompare.other_document(doc.path, other, rev)
            key = f"{doc.id}|{doc._revision}|{dependencies.revision(other_doc)}|{upto}"
            hit = self._compares.get(key)
            if hit is None:
                if not base.has_geometry:
                    raise pcompare.CompareError("the document has no geometry to compare")
                hit = pcompare.compare(base, evaluate(other_doc, upto=upto))
                hit = ({**hit[0], "hash": doc.hash, "revision": doc._revision}, hit[1])
                if len(self._compares) > 4:
                    self._compares.pop(next(iter(self._compares)))
                self._compares[key] = hit
            return hit

    def solve(self, doc: OpenDocument, sketch_name: str, drag: dict | None = None):
        """Preview: solve a sketch (with an optional drag) without writing anything."""
        with doc.lock:
            from .evaluate import sketch_context, solve_feature_sketch

            feature = doc.document.feature(sketch_name)
            if feature is None or feature.kind != "sketch":
                raise KeyError(f"no sketch named {sketch_name!r}")
            body, plane, identity = sketch_context(doc.document, feature, cache=doc._cache)
            targets = {k: (float(v[0]), float(v[1])) for k, v in (drag or {}).items()}
            return solve_feature_sketch(feature, body, targets or None, plane, identity)

    def set_source(self, doc: OpenDocument, source: str, base_hash: str | None) -> dict[str, Any]:
        return self.apply(doc, {"op": "replace_source", "source": source}, base_hash)

    def undo(self, doc: OpenDocument) -> dict[str, Any]:
        with doc.lock:
            self._check_disk(doc)
            if not doc.undo:
                return {"changed": False, "hash": doc.hash}
            old = doc.source
            self._commit(doc, doc.undo[-1], write=True, push_undo=False, event="changed")
            doc.undo.pop()
            doc.redo.append(old)
            return {"changed": True, "hash": doc.hash}

    def redo(self, doc: OpenDocument) -> dict[str, Any]:
        with doc.lock:
            self._check_disk(doc)
            if not doc.redo:
                return {"changed": False, "hash": doc.hash}
            old = doc.source
            self._commit(doc, doc.redo[-1], write=True, push_undo=False, event="changed")
            doc.redo.pop()
            doc.undo.append(old)
            return {"changed": True, "hash": doc.hash}

    def reload_from_disk(self, doc: OpenDocument) -> bool:
        """Called by the file watcher. Returns True if the file differed from memory."""
        with doc.lock:
            try:
                source = doc.path.read_text(encoding="utf-8")
            except OSError:
                return False
            if source == doc.source:
                return False
            self._commit(doc, source, write=False, push_undo=True, event="external")
            return True
