"""Turn a model file into a Document.

Two passes over the same text: libcst finds top-level statements, spans,
argument source text and read-only (loop-generated) features; executing the
file with the DSL produces the features with evaluated argument values. The
two are merged by feature name.
"""
from __future__ import annotations

import builtins
import math
import traceback
from dataclasses import dataclass, field
from typing import Any

import libcst as cst
import libcst.matchers as m
from libcst.metadata import MetadataWrapper, ParentNodeProvider, PositionProvider

from . import dsl
from .model import Document, DocumentError, Param

MATE_KINDS = {"fixed", "coincident", "concentric", "distance", "parallel", "angle"}
ASSEMBLY_KINDS = MATE_KINDS | {"instance"}
DRAWING_KINDS = {"view", "dimension", "note"}
FEATURE_KINDS = {
    "sketch", "extrude", "cut", "import_step", "plane", "revolve", "fillet", "chamfer", "shell",
    "linear_pattern", "circular_pattern", "mirror",
} | ASSEMBLY_KINDS | DRAWING_KINDS
BODY_KINDS = {"extrude", "cut", "revolve", "fillet", "chamfer", "shell", "linear_pattern", "circular_pattern", "mirror", "import_step"}
TOOL_KINDS = {"extrude", "cut", "revolve", "linear_pattern", "circular_pattern", "mirror"}  # repeatable by a pattern or mirror
ENTITY_KINDS = {"point", "line", "circle", "arc", "rect", "slot", "polygon", "project", "offset"}
CONSTRAINT_KINDS = {
    "coincident", "horizontal", "vertical", "parallel", "perpendicular", "equal", "tangent",
    "concentric", "coradial", "colinear", "symmetric", "midpoint", "on", "fix",
    "distance", "length", "diameter", "radius", "angle",
}
DIMENSION_KINDS = {"distance", "length", "diameter", "radius", "angle"}
SKETCH_KINDS = ENTITY_KINDS | CONSTRAINT_KINDS
NESTING = (
    cst.For, cst.While, cst.FunctionDef, cst.If, cst.With, cst.Try, cst.ClassDef,
    cst.ListComp, cst.SetComp, cst.DictComp, cst.GeneratorExp,
)
ALLOWED_MODULES = {"math", "plainsolid"}
# positional parameter names of DSL calls, so the tree can show `depth` for `extrude("e", s, 10)`
POSITIONAL = {
    "sketch": ("name", "on"),
    "extrude": ("name", "sketch", "depth"),
    "cut": ("name", "sketch", "depth"),
    "import_step": ("name", "path"),
    "plane": ("name", "base"),
    "revolve": ("name", "sketch", "axis"),
    "fillet": ("name", "edges", "radius"),
    "chamfer": ("name", "edges", "distance"),
    "shell": ("name", "faces", "thickness"),
    "linear_pattern": ("name", "feature", "count"),
    "circular_pattern": ("name", "feature", "count"),
    "mirror": ("name", "feature"),
    "instance": ("name", "path"),
    "fixed": ("name", "instance"),
    "view": ("name", "direction"),
    "dimension": ("name", "a", "b"),
    "note": ("name", "text"),
    "point": ("name", "at"),
    "line": ("name", "start", "end"),
    "circle": ("name", "diameter"),
    "arc": ("name", "center", "start", "end"),
    "rect": ("name", "width", "height"),
    "slot": ("name", "length", "width"),
    "polygon": ("name", "points"),
    "project": ("name", "selector"),
    "offset": ("name", "of", "distance"),
    "coincident": ("name", "a", "b"),
    "horizontal": ("name", "a", "b"),
    "vertical": ("name", "a", "b"),
    "parallel": ("name", "a", "b"),
    "perpendicular": ("name", "a", "b"),
    "equal": ("name", "a", "b"),
    "tangent": ("name", "a", "b"),
    "concentric": ("name", "a", "b"),
    "coradial": ("name", "a", "b"),
    "colinear": ("name", "a", "b"),
    "symmetric": ("name", "a", "b", "about"),
    "midpoint": ("name", "point", "line"),
    "on": ("name", "point", "curve"),
    "fix": ("name", "target"),
    "distance": ("name", "a", "b", "value"),
    "length": ("name", "line", "value"),
    "diameter": ("name", "circle", "value"),
    "radius": ("name", "circle", "value"),
    "angle": ("name", "a", "b", "value"),
}
_SAFE_NAMES = [
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance",
    "len", "list", "map", "max", "min", "print", "range", "reversed", "round", "set", "sorted",
    "str", "sum", "tuple", "zip", "Exception", "ValueError", "TypeError", "True", "False", "None",
]


@dataclass
class _Stmt:
    """What libcst knows about one top-level DSL statement."""

    name: str
    kind: str
    receiver: str | None
    variable: str | None
    start: int
    end: int
    read_only: bool
    arg_texts: dict[str, str] = field(default_factory=dict)


@dataclass
class _ParamStmt:
    name: str
    line: int
    expression: str


class _Collector(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider, ParentNodeProvider)

    def __init__(self, module: cst.Module):
        self.module = module
        self.stmts: list[_Stmt] = []
        self.params: list[_ParamStmt] = []

    def visit_Module(self, node: cst.Module) -> None:
        for stmt in node.body:
            if not isinstance(stmt, cst.SimpleStatementLine) or len(stmt.body) != 1:
                continue
            small = stmt.body[0]
            if not (
                isinstance(small, cst.Assign)
                and len(small.targets) == 1
                and isinstance(small.targets[0].target, cst.Name)
            ):
                continue
            if isinstance(small.value, cst.Call) and _classify(small.value) is not None:
                continue  # a feature statement, not a parameter
            pos = self.get_metadata(PositionProvider, stmt)
            value = small.value
            if m.matches(value, m.Call(func=m.Name("param"))) and value.args:
                value = value.args[0].value
            self.params.append(_ParamStmt(
                small.targets[0].target.value, pos.start.line, self.module.code_for_node(value)
            ))

    def visit_Call(self, node: cst.Call) -> None:
        cls = _classify(node)
        if cls is None:
            return
        kind, receiver = cls
        named = _name_arg(node, self.module)
        if named is None:
            return
        name, literal = named
        read_only, variable, stmt = False, None, None
        child: cst.CSTNode = node
        parent = self.get_metadata(ParentNodeProvider, node, None)
        while parent is not None:
            if isinstance(parent, NESTING):
                read_only = True
            if (
                isinstance(parent, cst.Assign)
                and parent.value is child
                and len(parent.targets) == 1
                and isinstance(parent.targets[0].target, cst.Name)
            ):
                variable = parent.targets[0].target.value
            if isinstance(parent, cst.SimpleStatementLine):
                stmt = parent
            child, parent = parent, self.get_metadata(ParentNodeProvider, parent, None)
        pos = self.get_metadata(PositionProvider, stmt or node)
        positional = POSITIONAL.get(kind, ())
        arg_texts = {}
        for i, a in enumerate(node.args):
            if a.keyword is not None:
                key = a.keyword.value
            elif i < len(positional):
                key = positional[i]
            else:
                key = str(i)
            arg_texts[key] = self.module.code_for_node(a.value)
        self.stmts.append(_Stmt(name, kind, receiver, variable, pos.start.line, pos.end.line,
                                read_only or not literal, arg_texts))


def _classify(call: cst.Call) -> tuple[str, str | None] | None:
    f = call.func
    if isinstance(f, cst.Name) and f.value in FEATURE_KINDS:
        return f.value, None
    if isinstance(f, cst.Attribute) and isinstance(f.value, cst.Name) and f.attr.value in SKETCH_KINDS:
        return f.attr.value, f.value.value
    return None


def _name_arg(call: cst.Call, module: cst.Module) -> tuple[str, bool] | None:
    if not call.args:
        return None
    a = call.args[0]
    if a.keyword is not None or a.star:
        return None
    if isinstance(a.value, cst.SimpleString):
        return a.value.evaluated_value, True
    return module.code_for_node(a.value), False


def collect(source: str) -> tuple[list[_Stmt], list[_ParamStmt]]:
    wrapper = MetadataWrapper(cst.parse_module(source))
    collector = _Collector(wrapper.module)
    wrapper.visit(collector)
    return collector.stmts, collector.params


# ---------------------------------------------------------------------------
# execution


def _safe_builtins() -> dict[str, Any]:
    safe = {name: getattr(builtins, name) for name in _SAFE_NAMES if hasattr(builtins, name)}

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if level != 0 or root not in ALLOWED_MODULES:
            raise ImportError(
                f"import of {name!r} is not allowed in a model file; only {sorted(ALLOWED_MODULES)} are"
            )
        return builtins.__import__(name, globals, locals, fromlist, level)

    safe["__import__"] = _import
    return safe


def _error_line(exc: BaseException, filename: str) -> int | None:
    line = None
    for fr in traceback.extract_tb(exc.__traceback__):
        if fr.filename == filename:
            line = fr.lineno
    return line


def parse_document(source: str, path: str | None = None) -> Document:
    filename = path or "<model>"
    doc = Document(source=source, path=path)

    try:
        stmts, param_stmts = collect(source)
    except cst.ParserSyntaxError as exc:
        doc.errors.append(DocumentError(f"syntax error: {exc.message}", exc.raw_line, "error"))
        return doc

    builder = dsl._Builder(filename)
    token = dsl._current.set(builder)
    namespace: dict[str, Any] = {
        "__name__": "__plainsolid__",
        "__file__": filename,
        "__builtins__": _safe_builtins(),
        "math": math,
    }
    try:
        code = compile(source, filename, "exec")
        exec(code, namespace)  # noqa: S102 - executing the user's own model file is the design
    except SyntaxError as exc:
        doc.errors.append(DocumentError(f"syntax error: {exc.msg}", exc.lineno, "error"))
    except Exception as exc:  # noqa: BLE001
        doc.errors.append(DocumentError(f"{type(exc).__name__}: {exc}", _error_line(exc, filename), "error"))
    finally:
        dsl._current.reset(token)

    built = builder.document
    doc.kind = built.kind
    doc.meta = built.meta
    doc.features = built.features

    by_name = {(s.name, s.kind): s for s in stmts if s.receiver is None}
    for f in doc.features:
        s = by_name.get((f.name, f.kind))
        if s is None:
            f.read_only = True  # created dynamically, not a top-level statement
            continue
        f.span = (s.start, s.end)
        f.read_only = s.read_only
        f.variable = s.variable
        f.arg_texts = s.arg_texts

    # entity and constraint statements: source text of their values, keyed by sketch variable
    by_receiver: dict[tuple[str, str], _Stmt] = {(s.receiver, s.name): s for s in stmts if s.receiver is not None}
    for f in doc.features:
        if f.kind != "sketch" or not f.variable:
            continue
        for e in f.entities:
            st = by_receiver.get((f.variable, e.name))
            if st is not None:
                e.arg_texts = st.arg_texts
                e.line = st.start
        for c in f.constraints:
            st = by_receiver.get((f.variable, c.name))
            if st is not None:
                c.value_text = st.arg_texts.get("value")
                c.line = st.start

    for ps in param_stmts:
        value = namespace.get(ps.name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        doc.params.append(Param(
            name=ps.name, value=float(value), description=getattr(value, "description", ""),
            line=ps.line, expression=ps.expression,
        ))
    return doc


def parse_file(path: str) -> Document:
    with open(path, encoding="utf-8") as fh:
        return parse_document(fh.read(), path)
