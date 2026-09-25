"""File dependencies and geometry revisions, without evaluating the CAD kernel."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .model import Document, source_hash


def file_stamp(path: Path) -> tuple[str, int, int, int]:
    path = path.resolve()
    try:
        st = path.stat()
        return str(path), st.st_mtime_ns, st.st_ctime_ns, st.st_size
    except OSError:
        return str(path), -1, -1, -1


def references(doc: Document) -> list[Path]:
    """The files a document reads, memoized on the document: a parsed document never changes,
    and a large assembly asks for its revision several times per request."""
    cached = doc.__dict__.get("_references")
    if cached is None:
        base = Path(doc.path).parent if doc.path else Path.cwd()
        paths = [f.args.get("path") for f in doc.features if f.kind in ("import_step", "instance")]
        paths += [e.args.get("path") for f in doc.features for e in f.entities if e.kind == "import_dxf"]
        paths += [f.args.get("dxf") for f in doc.features if f.kind == "view"]
        if doc.kind == "drawing":
            paths.append(doc.meta.get("of"))
        seen: dict[Path, None] = {}
        for p in paths:
            if isinstance(p, str) and p:
                seen[(base / p.split("#", 1)[0]).resolve()] = None
        cached = doc.__dict__["_references"] = tuple(seen)
    return list(cached)


@lru_cache(maxsize=256)
def _references(stamp: tuple[str, int, int, int]) -> tuple[Path, ...]:
    from .parse import parse_file

    path = Path(stamp[0])
    if path.suffix != ".py" or stamp[1] == -1:
        return ()
    try:
        return tuple(references(parse_file(str(path))))
    except (OSError, UnicodeError):
        return ()


def file_dependencies(paths: list[Path]) -> tuple[tuple[str, int, int, int], ...]:
    """Include missing inputs and transitive imports, visiting cycles only once."""
    found = {}
    pending = list(paths)
    while pending:
        path = pending.pop().resolve()
        if str(path) in found:
            continue
        stamp = file_stamp(path)
        found[str(path)] = stamp
        pending.extend(_references(stamp))
    return tuple(sorted(found.values()))


def revision(doc: Document) -> str:
    return source_hash(json.dumps([doc.path, doc.hash, file_dependencies(references(doc))]))
