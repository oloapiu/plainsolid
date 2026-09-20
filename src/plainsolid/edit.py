"""Edit operations: the only way any client changes a model file.

Each operation takes source text and returns new source text, patched
surgically with libcst so comments and formatting survive. `apply` maps a
JSON operation to these functions and also composes DSL statements from
structured arguments, so a GUI never has to write Python.
"""
from __future__ import annotations

import difflib
import keyword
from dataclasses import dataclass
from typing import Any

import libcst as cst
import libcst.matchers as m

from .literals import python_literal

MATE_KINDS = {"fixed", "coincident", "concentric", "distance", "parallel", "angle"}
DRAWING_KINDS = {"view", "dimension", "note"}
FEATURE_KINDS = {
    "sketch", "extrude", "cut", "import_step", "plane", "revolve", "fillet", "chamfer", "shell",
    "linear_pattern", "circular_pattern", "mirror", "instance",
} | MATE_KINDS | DRAWING_KINDS
ENTITY_KINDS = {"point", "line", "circle", "arc", "rect", "slot", "polygon", "project", "offset"}
CONSTRAINT_KINDS = {
    "coincident", "horizontal", "vertical", "parallel", "perpendicular", "equal", "tangent",
    "concentric", "coradial", "colinear", "symmetric", "midpoint", "on", "fix",
    "distance", "length", "diameter", "radius", "angle",
}
SKETCH_KINDS = ENTITY_KINDS | CONSTRAINT_KINDS


class EditError(ValueError):
    pass


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


# --- statement lookup ----------------------------------------------------------

def _stmt_call(stmt: cst.CSTNode) -> cst.Call | None:
    """The DSL call of a top-level simple statement `x = call(...)` or `call(...)`."""
    if not isinstance(stmt, cst.SimpleStatementLine) or len(stmt.body) != 1:
        return None
    small = stmt.body[0]
    if isinstance(small, (cst.Expr, cst.Assign)) and isinstance(small.value, cst.Call):
        return small.value
    return None


def _feature_index(module: cst.Module, name: str, kind: str | None = None) -> int:
    for i, stmt in enumerate(module.body):
        call = _stmt_call(stmt)
        if call is None:
            continue
        cls = _classify(call)
        if cls is None or cls[1] is not None or (kind is not None and cls[0] != kind):
            continue  # entity and constraint statements have a receiver; features do not
        named = _name_arg(call, module)
        if named is not None and named[1] and named[0] == name:
            return i
    raise EditError(f"no top-level feature named {name!r}")


def _replace_call(stmt: cst.SimpleStatementLine, call: cst.Call) -> cst.SimpleStatementLine:
    return stmt.with_changes(body=[stmt.body[0].with_changes(value=call)])


def _param_index(module: cst.Module, name: str) -> int:
    for i, stmt in enumerate(module.body):
        if not isinstance(stmt, cst.SimpleStatementLine) or len(stmt.body) != 1:
            continue
        small = stmt.body[0]
        if (
            isinstance(small, cst.Assign)
            and len(small.targets) == 1
            and m.matches(small.targets[0].target, m.Name(name))
        ):
            return i
    raise EditError(f"no top-level parameter named {name!r}")


# --- edit operations -----------------------------------------------------------

# keyword arguments whose default value is better left unwritten: setting one of
# these back to its default removes the keyword instead of writing `flip=False`
DEFAULTS = {
    "construction": ("False",), "flip": ("False",), "symmetric": ("False",), "through": ("False",),
    "draft": ("0", "0.0"), "offset": ("0", "0.0"), "inside": ("False",), "suppressed": ("False",),
    "outward": ("False",), "op": ('"add"', "'add'"), "distance2": ("None",), "count2": ("None",),
    "spacing2": ("None",), "tolerance": ("None",), "material": ("None",), "density": ("None",), "color": ("None",),
}
# the same, where the default depends on the statement kind
KIND_DEFAULTS = {
    ("plane", "angle"): ("0", "0.0"), ("slot", "angle"): ("0", "0.0"),
    ("revolve", "angle"): ("360", "360.0"), ("circular_pattern", "angle"): ("360", "360.0"),
    ("project", "construction"): ("True",),  # converted geometry is construction unless the file says otherwise
    ("offset", "side"): ('"outside"', "'outside'"), ("offset", "corners"): ('"sharp"', "'sharp'"),
    ("instance", "at"): ("(0, 0, 0)", "(0.0, 0.0, 0.0)"), ("instance", "rotate"): ("(0, 0, 0)", "(0.0, 0.0, 0.0)"),
    ("view", "hidden"): ("None",), ("view", "scale"): ("None",), ("view", "section"): ("None",),
    ("dimension", "kind"): ("None",), ("dimension", "along"): ("None",), ("dimension", "text"): ("None",),
    ("note", "size"): ("3.5",), ("note", "view"): ("None",),
    # sketch dimensions: the label's place and the distance/angle options
    **{(k, "at"): ("None",) for k in ("distance", "length", "diameter", "radius", "angle")},
    ("distance", "along"): ("None",), ("angle", "reverse"): ("False",),
    # macro corners: none rounded, none bevelled
    **{(k, arg): ("None", "0", "0.0", "{}") for k in ("rect", "polygon") for arg in ("corners", "chamfers")},
}


def is_default(kind: str | None, kwarg: str, text: str) -> bool:
    return text in KIND_DEFAULTS.get((kind, kwarg), DEFAULTS.get(kwarg, ()))


def _set_or_drop(args: list[cst.Arg], j: int, kwarg: str, new_value: cst.BaseExpression, text: str,
                 kind: str | None = None) -> list[cst.Arg]:
    if args[j].keyword is not None and is_default(kind, kwarg, text):
        out = args[:j] + args[j + 1:]
        if out and j == len(out):
            out[-1] = out[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        return out
    args[j] = args[j].with_changes(value=new_value)
    return args


def set_argument(src: str, feature_name: str, kwarg: str, new_expr_text: str) -> str:
    from .parse import POSITIONAL

    module = cst.parse_module(src)
    idx = _feature_index(module, feature_name)
    stmt = module.body[idx]
    call = _stmt_call(stmt)
    kind = _classify(call)[0]
    positional = POSITIONAL.get(kind, ())
    new_value = cst.parse_expression(new_expr_text)
    args = list(call.args)
    for j, a in enumerate(args):
        key = a.keyword.value if a.keyword is not None else (positional[j] if j < len(positional) else None)
        if key == kwarg:
            args = _set_or_drop(args, j, kwarg, new_value, new_expr_text, kind)
            break
    else:
        if is_default(kind, kwarg, new_expr_text):
            return src  # already at the default
        if args:
            args[-1] = args[-1].with_changes(
                comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
            )
        args.append(cst.Arg(
            keyword=cst.Name(kwarg), value=new_value,
            equal=cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace("")),
        ))
    body = list(module.body)
    body[idx] = _replace_call(stmt, call.with_changes(args=args))
    return module.with_changes(body=body).code


def get_argument(src: str, feature_name: str, kwarg: str) -> str | None:
    module = cst.parse_module(src)
    call = _stmt_call(module.body[_feature_index(module, feature_name)])
    for a in call.args:
        if a.keyword is not None and a.keyword.value == kwarg:
            return module.code_for_node(a.value)
    return None


def add_feature(src: str, after_feature_name: str, statement_text: str) -> str:
    return add_features(src, after_feature_name, [statement_text])


def add_features(src: str, after_feature_name: str, statements: list[str]) -> str:
    """Insert several statements after a feature in one pass (hundreds at a time when
    a review import turns into instances)."""
    module = cst.parse_module(src)
    idx = _feature_index(module, after_feature_name)
    body = list(module.body)
    body[idx + 1:idx + 1] = [cst.parse_statement(text) for text in statements]
    return module.with_changes(body=body).code


class _NameUse(cst.CSTVisitor):
    def __init__(self, names: set[str]):
        self.names = names
        self.found = False

    def visit_Name(self, node: cst.Name) -> None:
        if node.value in self.names:
            self.found = True


def _uses(node: cst.CSTNode, names: set[str]) -> bool:
    v = _NameUse(names)
    node.visit(v)
    return v.found


def _stmt_variable(stmt: cst.CSTNode) -> str | None:
    if isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1:
        small = stmt.body[0]
        if isinstance(small, cst.Assign) and len(small.targets) == 1 and isinstance(small.targets[0].target, cst.Name):
            return small.targets[0].target.value
    return None


class _AllNames(cst.CSTVisitor):
    def __init__(self):
        self.names: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)


@dataclass
class _Stmt:
    """What the cascade needs to know about one DSL statement, gathered in one parse."""

    index: int
    receiver: str | None
    label: str
    literal: bool
    variable: str | None
    names: set[str]  # every Name in the call
    strings: list[str]  # string arguments after the name: entity references


def _statements(module: cst.Module) -> list[_Stmt]:
    out = []
    for i, stmt in enumerate(module.body):
        call = _stmt_call(stmt)
        if call is None:
            continue
        cls = _classify(call)
        if cls is None:
            continue
        named = _name_arg(call, module)
        v = _AllNames()
        call.visit(v)
        strings = [a.value.evaluated_value for a in call.args[1:] if isinstance(a.value, cst.SimpleString)]
        out.append(_Stmt(i, cls[1], named[0] if named else "?", bool(named and named[1]), _stmt_variable(stmt), v.names, strings))
    return out


def _cascade(stmts: list[_Stmt], start: _Stmt) -> dict[str, Any]:
    doomed = {start.index}
    names = {start.variable} if start.variable else set()
    features, entities = [], []
    removed_entities: set[tuple[str, str]] = set()  # (sketch variable, entity name)
    changed = True
    while changed:
        changed = False
        for s in stmts:
            if s.index in doomed or s.index <= start.index:
                continue
            if s.receiver is not None:
                # an entity or constraint of a sketch: goes when its sketch goes, when it
                # uses a doomed variable (a projection of a deleted body), or when it is a
                # constraint naming a removed entity of the same sketch
                mine = {name for sk, name in removed_entities if sk == s.receiver}
                hit = (s.receiver in names or bool(s.names & names)
                       or any(t in mine or t.split(".")[0] in mine for t in s.strings))
            else:
                hit = bool(s.names & names)
            if not hit:
                continue
            doomed.add(s.index)
            changed = True
            if s.receiver is not None:
                entities.append(s.label)
                removed_entities.add((s.receiver, s.label))
            else:
                features.append(s.label)
                if s.variable and s.variable not in names:
                    names.add(s.variable)
    return {"indices": sorted(doomed), "features": features, "entities": entities}


def dependents(src: str, feature_name: str) -> dict[str, Any]:
    """Everything that must go with a feature: its own entity and constraint
    statements, and every later feature statement that uses its variable,
    recursively. Returns statement indices plus the feature and entity names."""
    module = cst.parse_module(src)
    stmts = _statements(module)
    start = next((s for s in stmts if s.receiver is None and s.literal and s.label == feature_name), None)
    if start is None:
        raise EditError(f"no top-level feature named {feature_name!r}")
    return _cascade(stmts, start)


def dependents_all(src: str) -> dict[str, dict[str, Any]]:
    """dependents() for every top-level feature, from one parse: the tree of a
    proposal with hundreds of instances asks for all of them at once."""
    stmts = _statements(cst.parse_module(src))
    return {s.label: _cascade(stmts, s) for s in stmts if s.receiver is None and s.literal}


def delete_feature(src: str, feature_name: str, cascade: bool = True) -> str:
    """Delete a feature statement. With cascade (the default) its entity and
    constraint statements and the features that depend on it go too, so the
    file never references an undefined name."""
    module = cst.parse_module(src)
    info = dependents(src, feature_name) if cascade else {"indices": [_feature_index(module, feature_name)]}
    body = list(module.body)
    footer = list(module.footer)
    for i in sorted(info["indices"], reverse=True):
        removed = body.pop(i)
        # Keep blank lines and comments that sat above the deleted statement: they are
        # user text the op was not asked to remove.
        kept = [ln for ln in removed.leading_lines if ln.comment is not None or i < len(body)]
        if i < len(body):
            body[i] = body[i].with_changes(leading_lines=[*removed.leading_lines, *body[i].leading_lines])
        elif kept:
            footer = [*kept, *footer]
    return module.with_changes(body=body, footer=footer).code


def add_sketch_entity(src: str, sketch_name: str, statement_text: str) -> str:
    module = cst.parse_module(src)
    idx = _feature_index(module, sketch_name, kind="sketch")
    small = module.body[idx].body[0]
    if not isinstance(small, cst.Assign) or not isinstance(small.targets[0].target, cst.Name):
        raise EditError(f"sketch {sketch_name!r} is not bound to a variable")
    var = small.targets[0].target.value
    # Insert after the last entity statement of this sketch (a call on the sketch
    # variable), not after any statement mentioning it: an extrude consuming the
    # sketch must stay below the sketch's entities.
    last = idx
    for i in range(idx + 1, len(module.body)):
        call = _stmt_call(module.body[i])
        if call is not None and m.matches(call.func, m.Attribute(value=m.Name(var))):
            last = i
    body = list(module.body)
    body.insert(last + 1, cst.parse_statement(statement_text))
    return module.with_changes(body=body).code


def _is_int_literal(node: cst.BaseExpression) -> bool:
    if isinstance(node, cst.UnaryOperation) and isinstance(node.operator, cst.Minus):
        node = node.expression
    return isinstance(node, cst.Integer)


def _is_float_literal(node: cst.BaseExpression) -> bool:
    if isinstance(node, cst.UnaryOperation) and isinstance(node.operator, cst.Minus):
        node = node.expression
    return isinstance(node, cst.Float)


def set_parameter(src: str, param_name: str, new_value_text: str) -> str:
    """Set a parameter's value (the first argument when it is a `param(...)` call).
    A whole number written over a float literal keeps the float style (`2.0`)."""
    module = cst.parse_module(src)
    idx = _param_index(module, param_name)
    stmt = module.body[idx]
    small = stmt.body[0]
    new_value = cst.parse_expression(new_value_text)
    value = small.value
    old_value = value.args[0].value if m.matches(value, m.Call(func=m.Name("param"))) and value.args else value
    if _is_float_literal(old_value) and _is_int_literal(new_value):
        new_value = cst.parse_expression(new_value_text.strip() + ".0")
    if m.matches(value, m.Call(func=m.Name("param"))) and value.args:
        args = list(value.args)
        args[0] = args[0].with_changes(value=new_value)
        value = value.with_changes(args=args)
    else:
        value = new_value
    body = list(module.body)
    body[idx] = stmt.with_changes(body=[small.with_changes(value=value)])
    return module.with_changes(body=body).code


def _is_param_stmt(stmt: cst.CSTNode) -> bool:
    """A top-level `name = number-or-expression` or `name = param(...)`, not a feature assignment."""
    if not isinstance(stmt, cst.SimpleStatementLine) or len(stmt.body) != 1:
        return False
    small = stmt.body[0]
    if not isinstance(small, cst.Assign) or len(small.targets) != 1 or not isinstance(small.targets[0].target, cst.Name):
        return False
    if isinstance(small.value, cst.Call):
        return m.matches(small.value.func, m.Name("param"))
    return True


def add_parameter(src: str, name: str, value_text: str, description: str | None = None) -> str:
    """Add a parameter after the last one (so every feature can use it), or after the
    meta() line, or after the imports when the file has neither."""
    if not name.isidentifier() or keyword.iskeyword(name):
        raise EditError(f"{name!r} is not a valid parameter name")
    module = cst.parse_module(src)
    try:
        _param_index(module, name)
    except EditError:
        pass
    else:
        raise EditError(f"{name!r} already exists; use set_parameter to change it")
    try:
        cst.parse_expression(value_text)
    except cst.ParserSyntaxError as exc:
        raise EditError(f"bad value for {name}: {exc.message}") from None
    text = f"{name} = param({value_text}, {python_literal(description)})" if description else f"{name} = {value_text}"
    body = list(module.body)
    after = -1
    for i, stmt in enumerate(body):
        if _is_param_stmt(stmt):
            after = i
    if after < 0:
        for i, stmt in enumerate(body):
            call = _stmt_call(stmt)
            if call is not None and m.matches(call.func, m.Name("meta")):
                after = i
                break
    if after < 0:
        while after + 1 < len(body) and isinstance(body[after + 1], cst.SimpleStatementLine) and \
                all(isinstance(x, (cst.Import, cst.ImportFrom)) for x in body[after + 1].body):
            after += 1
    body.insert(after + 1, cst.parse_statement(text))
    return module.with_changes(body=body).code


def delete_parameter(src: str, name: str) -> str:
    """Remove a parameter that nothing uses; a used one is refused with its users."""
    module = cst.parse_module(src)
    idx = _param_index(module, name)
    if not _is_param_stmt(module.body[idx]):
        raise EditError(f"{name!r} is a feature, not a parameter; use delete_feature")
    users = []
    for i, stmt in enumerate(module.body):
        if i != idx and _uses(stmt, {name}):
            users.append(module.code_for_node(stmt).strip().splitlines()[0])
    if users:
        shown = "; ".join(users[:3]) + ("; ..." if len(users) > 3 else "")
        raise EditError(f"{name!r} is still used: {shown}")
    body = list(module.body)
    del body[idx]
    return module.with_changes(body=body).code


def set_meta(src: str, key: str, new_value_text: str) -> str:
    """Set one keyword of the top-level meta(...) call: replaced when present, appended
    when missing, dropped when the new value is None. A file without a meta line gets
    one after the import. The document's kind is not changed this way."""
    if not key.isidentifier():
        raise EditError(f"meta key must be an identifier, got {key!r}")
    if key == "kind":
        raise EditError("the document kind is fixed when the file is created; make a new file instead")
    module = cst.parse_module(src)
    body = list(module.body)
    idx = next((i for i, stmt in enumerate(body)
                if (call := _stmt_call(stmt)) is not None and m.matches(call.func, m.Name("meta"))), None)
    if idx is None:
        if new_value_text == "None":
            return src
        after = next((i for i, stmt in enumerate(body) if isinstance(stmt, cst.SimpleStatementLine)
                      and isinstance(stmt.body[0], (cst.ImportFrom, cst.Import))), -1)
        body.insert(after + 1, cst.parse_statement(f"meta({key}={new_value_text})"))
        return module.with_changes(body=body).code
    stmt = body[idx]
    call = _stmt_call(stmt)
    args = list(call.args)
    j = next((k for k, a in enumerate(args) if a.keyword is not None and a.keyword.value == key), None)
    if new_value_text == "None":
        if j is None:
            return src
        args = args[:j] + args[j + 1:]
        if args and j == len(args):
            args[-1] = args[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
    else:
        new_value = cst.parse_expression(new_value_text)
        if j is not None:
            args[j] = args[j].with_changes(value=new_value)
        else:
            if args:
                args[-1] = args[-1].with_changes(comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")))
            args.append(cst.Arg(keyword=cst.Name(key), value=new_value,
                                equal=cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace(""))))
    body[idx] = _replace_call(stmt, call.with_changes(args=args))
    return module.with_changes(body=body).code


def get_parameter(src: str, param_name: str) -> str:
    module = cst.parse_module(src)
    value = module.body[_param_index(module, param_name)].body[0].value
    if m.matches(value, m.Call(func=m.Name("param"))) and value.args:
        value = value.args[0].value
    return module.code_for_node(value)



def append_statement(src: str, statement_text: str) -> str:
    module = cst.parse_module(src)
    body = [*module.body, cst.parse_statement(statement_text)]
    return module.with_changes(body=body).code


def _sketch_var(module: cst.Module, sketch_name: str) -> str:
    idx = _feature_index(module, sketch_name, kind="sketch")
    small = module.body[idx].body[0]
    if not isinstance(small, cst.Assign) or not isinstance(small.targets[0].target, cst.Name):
        raise EditError(f"sketch {sketch_name!r} is not bound to a variable")
    return small.targets[0].target.value


def _sketch_statements(module: cst.Module, sketch_name: str) -> list[tuple[int, cst.Call, str, str]]:
    """(index, call, kind, name) of every entity or constraint statement of the sketch."""
    var = _sketch_var(module, sketch_name)
    idx = _feature_index(module, sketch_name, kind="sketch")
    out = []
    for i in range(idx + 1, len(module.body)):
        call = _stmt_call(module.body[i])
        if call is None or not m.matches(call.func, m.Attribute(value=m.Name(var))):
            continue
        named = _name_arg(call, module)
        if named is not None and named[1]:
            out.append((i, call, call.func.attr.value, named[0]))
    return out


def delete_sketch_entity(src: str, sketch_name: str, entity_name: str) -> str:
    module = cst.parse_module(src)
    idx = _feature_index(module, sketch_name, kind="sketch")
    small = module.body[idx].body[0]
    if not isinstance(small, cst.Assign) or not isinstance(small.targets[0].target, cst.Name):
        raise EditError(f"sketch {sketch_name!r} is not bound to a variable")
    var = small.targets[0].target.value
    for i in range(idx + 1, len(module.body)):
        call = _stmt_call(module.body[i])
        if call is None or not m.matches(call.func, m.Attribute(value=m.Name(var))):
            continue
        named = _name_arg(call, module)
        if named is not None and named[1] and named[0] == entity_name:
            body = list(module.body)
            removed = body.pop(i)
            if i < len(body):
                body[i] = body[i].with_changes(leading_lines=[*removed.leading_lines, *body[i].leading_lines])
            return module.with_changes(body=body).code
    raise EditError(f"no entity {entity_name!r} in sketch {sketch_name!r}")


def set_entity_argument(src: str, sketch_name: str, entity_name: str, kwarg: str, new_expr_text: str) -> str:
    """Set an argument on a sketch entity or constraint statement (positional
    args are matched by the DSL's positional names)."""
    from .parse import POSITIONAL

    module = cst.parse_module(src)
    idx = _feature_index(module, sketch_name, kind="sketch")
    var = module.body[idx].body[0].targets[0].target.value
    for i in range(idx + 1, len(module.body)):
        call = _stmt_call(module.body[i])
        if call is None or not m.matches(call.func, m.Attribute(value=m.Name(var))):
            continue
        named = _name_arg(call, module)
        if named is None or not named[1] or named[0] != entity_name:
            continue
        kind = call.func.attr.value
        positional = POSITIONAL.get(kind, ())
        new_value = cst.parse_expression(new_expr_text)
        args = list(call.args)
        for j, a in enumerate(args):
            key = a.keyword.value if a.keyword is not None else (positional[j] if j < len(positional) else None)
            if key == kwarg:
                args = _set_or_drop(args, j, kwarg, new_value, new_expr_text, kind)
                break
        else:
            if is_default(kind, kwarg, new_expr_text):
                return src  # already at the default
            if args:
                args[-1] = args[-1].with_changes(comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")))
            args.append(cst.Arg(keyword=cst.Name(kwarg), value=new_value,
                                equal=cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace(""))))
        body = list(module.body)
        body[i] = _replace_call(module.body[i], call.with_changes(args=args))
        return module.with_changes(body=body).code
    raise EditError(f"no entity {entity_name!r} in sketch {sketch_name!r}")


# --- coordinate write-back ---------------------------------------------------------

def _num_text(v: float, precision: int) -> str:
    r = round(float(v), precision)
    if r == int(r):
        return str(int(r))
    return repr(r)


def _is_numeric_literal(node: cst.BaseExpression) -> bool:
    if isinstance(node, (cst.Integer, cst.Float)):
        return True
    if isinstance(node, cst.UnaryOperation) and isinstance(node.operator, cst.Minus):
        return isinstance(node.expression, (cst.Integer, cst.Float))
    return False


def _literal_value(node: cst.BaseExpression) -> float:
    if isinstance(node, cst.UnaryOperation):
        return -_literal_value(node.expression)
    return float(node.value.replace("_", ""))


def _rewrite_number(node: cst.BaseExpression, value: float, precision: int) -> cst.BaseExpression | None:
    """A new literal node when the value changed beyond the precision, else None."""
    if not _is_numeric_literal(node):
        return None
    if abs(_literal_value(node) - value) < 0.5 * 10 ** (-precision):
        return None
    return cst.parse_expression(_num_text(value, precision))


def _rewrite_point(node: cst.BaseExpression, value, precision: int) -> cst.BaseExpression | None:
    if not isinstance(node, (cst.Tuple, cst.List)) or len(node.elements) != len(value):
        return None
    changed = False
    elements = list(node.elements)
    for i, (el, v) in enumerate(zip(elements, value, strict=True)):
        new = _rewrite_number(el.value, v, precision)
        if new is not None:
            elements[i] = el.with_changes(value=new)
            changed = True
    return node.with_changes(elements=elements) if changed else None


def write_back(src: str, sketch_name: str, coords: dict[str, dict[str, Any]], precision: int = 4) -> str:
    """Write solved coordinates into the entity statements of a sketch. Only plain
    numeric literals are touched; expressions stay as the user wrote them."""
    from .parse import POSITIONAL

    module = cst.parse_module(src)
    body = list(module.body)
    changed_any = False
    for i, call, kind, name in _sketch_statements(module, sketch_name):
        solved = coords.get(name)
        if not solved or kind not in ENTITY_KINDS:
            continue
        positional = POSITIONAL.get(kind, ())
        args = list(call.args)
        changed = False
        for j, a in enumerate(args):
            key = a.keyword.value if a.keyword is not None else (positional[j] if j < len(positional) else None)
            if key not in solved:
                continue
            value = solved[key]
            if key == "points" and isinstance(a.value, (cst.List, cst.Tuple)):
                elements = list(a.value.elements)
                pts_changed = False
                for pi, (el, pv) in enumerate(zip(elements, value, strict=False)):
                    new = _rewrite_point(el.value, pv, precision)
                    if new is not None:
                        elements[pi] = el.with_changes(value=new)
                        pts_changed = True
                if pts_changed:
                    args[j] = a.with_changes(value=a.value.with_changes(elements=elements))
                    changed = True
                continue
            if key in ("corners", "chamfers"):
                new = _rewrite_corners(a.value, value, precision)
            else:
                new = _rewrite_point(a.value, value, precision) if isinstance(value, (tuple, list)) \
                    else _rewrite_number(a.value, value, precision)
            if new is not None:
                args[j] = a.with_changes(value=new)
                changed = True
        if changed:
            body[i] = _replace_call(module.body[i], call.with_changes(args=args))
            changed_any = True
    return module.with_changes(body=body).code if changed_any else src


def _rewrite_corners(node: cst.BaseExpression, value: Any, precision: int) -> cst.BaseExpression | None:
    """corners= written back: a number stays a number while the solver reports one, a dict has
    each corner's literal rewritten, and the form switches when the radii stop agreeing."""
    if isinstance(value, (int, float)):
        return _rewrite_number(node, float(value), precision) if not isinstance(node, cst.Dict) \
            else cst.parse_expression(_num_text(float(value), precision))
    if not isinstance(value, dict):
        return None
    if not isinstance(node, cst.Dict):
        return cst.parse_expression(python_literal({k: round(float(v), precision) for k, v in value.items()}))
    changed = False
    elements = list(node.elements)
    for i, el in enumerate(elements):
        if not isinstance(el, cst.DictElement) or not isinstance(el.key, cst.SimpleString):
            continue
        k = el.key.evaluated_value
        if k in value:
            new = _rewrite_number(el.value, float(value[k]), precision)
            if new is not None:
                elements[i] = el.with_changes(value=new)
                changed = True
    return node.with_changes(elements=elements) if changed else None


def _tuple_text(values, precision: int) -> str:
    return "(" + ", ".join(_num_text(v, precision) for v in values) + ")"


def write_poses(src: str, poses: dict[str, dict[str, Any]], precision: int = 4) -> str:
    """Write solved poses into instance statements: `at=` and `rotate=` literals are
    rewritten where they changed, added when they leave the default, and left
    alone when the user wrote an expression."""
    from .parse import POSITIONAL

    module = cst.parse_module(src)
    body = list(module.body)
    changed_any = False
    positional = POSITIONAL["instance"]
    for i, stmt in enumerate(module.body):
        call = _stmt_call(stmt)
        if call is None:
            continue
        cls = _classify(call)
        if cls is None or cls[0] != "instance" or cls[1] is not None:
            continue
        named = _name_arg(call, module)
        if named is None or not named[1] or named[0] not in poses:
            continue
        target = poses[named[0]]
        args = list(call.args)
        changed = False
        present: dict[str, int] = {}
        for j, a in enumerate(args):
            key = a.keyword.value if a.keyword is not None else (positional[j] if j < len(positional) else None)
            if key in ("at", "rotate"):
                present[key] = j
        for key in ("at", "rotate"):
            value = target.get(key)
            if value is None:
                continue
            if key in present:
                j = present[key]
                new = _rewrite_point(args[j].value, value, precision)
                if new is not None:
                    args[j] = args[j].with_changes(value=new)
                    changed = True
            elif any(abs(round(float(c), precision)) > 0 for c in value):
                if args:
                    args[-1] = args[-1].with_changes(comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")))
                args.append(cst.Arg(keyword=cst.Name(key), value=cst.parse_expression(_tuple_text(value, precision)),
                                    equal=cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace(""))))
                changed = True
        if changed:
            body[i] = _replace_call(module.body[i], call.with_changes(args=args))
            changed_any = True
    return module.with_changes(body=body).code if changed_any else src


# --- composing statements from structured arguments ------------------------------

def _lit(value: Any) -> str:
    """Python source for a literal or a pass-through expression string."""
    try:
        return python_literal(value)
    except ValueError as exc:
        raise EditError(str(exc)) from None


def _expr(value: Any) -> str:
    """Source for a value that may be a pass-through expression ({"expr": ...}) or a literal."""
    return value["expr"] if isinstance(value, dict) and "expr" in value else _lit(value)


def _bare(value: Any, names: tuple[str, ...]) -> str:
    """Global axes and standard planes are written bare (X, YZ), other values as expressions."""
    if isinstance(value, str) and value in names:
        return value
    return _expr(value)


_AXES = ("X", "Y", "Z", "-X", "-Y", "-Z")
_PLANES = ("XY", "XZ", "YZ")


def compose_feature(kind: str, name: str, args: dict[str, Any]) -> str:
    """A top-level DSL statement for a feature."""
    call = _compose_call(kind, name, args)
    if args.get("suppressed"):
        call = call[:-1] + ", suppressed=True)"
    return call if kind in ("cut", "dimension", "note") or kind in MATE_KINDS else f"{name} = {call}"


def _compose_call(kind: str, name: str, args: dict[str, Any]) -> str:
    if kind == "sketch":
        on = args.get("on", "XY")
        rest = [f"on={_bare(on, _PLANES)}"]
        if args.get("offset"):
            rest.append(f"offset={_lit(args['offset'])}")
        if args.get("flip"):
            rest.append("flip=True")
        return f"sketch({_lit(name)}, {', '.join(rest)})"
    if kind == "plane":
        base = args.get("base")
        parts = [_lit(name)]
        if base is not None:
            parts.append(_bare(base, _PLANES))
        for key in ("offset", "angle", "about", "between", "through", "flip"):
            if key in args and args[key] not in (None, False, 0, 0.0):
                parts.append(f"{key}={_expr(args[key])}")
        return f"plane({', '.join(parts)})"
    if kind in ("extrude", "cut"):
        parts = [_lit(name), str(args["sketch"])]
        if args.get("depth") is not None:
            parts.append(_lit(args["depth"]))
        elif kind == "extrude" and not args.get("upto") and not args.get("through"):
            raise EditError("extrude needs a depth, upto= or through=True")
        for key in ("upto", "through", "symmetric", "flip", "draft", "op"):
            if key in args and args[key] not in (None, False, 0, 0.0, "add"):
                parts.append(f"{key}={_expr(args[key])}")
        return f"{kind}({', '.join(parts)})"
    if kind == "revolve":
        parts = [_lit(name), str(args["sketch"]), _bare(args["axis"], _AXES)]
        if args.get("angle") not in (None, 360, 360.0):
            parts.append(f"angle={_lit(args['angle'])}")
        if args.get("op") not in (None, "add"):
            parts.append(f"op={_lit(args['op'])}")
        return f"revolve({', '.join(parts)})"
    if kind == "fillet":
        return f"fillet({_lit(name)}, {_expr(args['edges'])}, {_lit(args['radius'])})"
    if kind == "chamfer":
        parts = [_lit(name), _expr(args["edges"]), _lit(args["distance"])]
        if args.get("distance2") is not None:
            parts.append(f"distance2={_lit(args['distance2'])}")
        return f"chamfer({', '.join(parts)})"
    if kind == "shell":
        faces = args.get("faces")
        parts = [_lit(name), "None" if faces is None else _expr(faces), _lit(args["thickness"])]
        if args.get("outward"):
            parts.append("outward=True")
        return f"shell({', '.join(parts)})"
    if kind == "linear_pattern":
        parts = [_lit(name), _expr(args["feature"]), _lit(int(args["count"])), f"spacing={_lit(args['spacing'])}",
                 f"direction={_bare(args.get('direction', 'X'), _AXES)}"]
        if args.get("count2") is not None:
            parts += [f"count2={_lit(int(args['count2']))}", f"spacing2={_lit(args['spacing2'])}",
                      f"direction2={_bare(args.get('direction2', 'Y'), _AXES)}"]
        return f"linear_pattern({', '.join(parts)})"
    if kind == "circular_pattern":
        parts = [_lit(name), _expr(args["feature"]), _lit(int(args["count"])), f"axis={_bare(args.get('axis', 'Z'), _AXES)}"]
        if args.get("angle") not in (None, 360, 360.0):
            parts.append(f"angle={_lit(args['angle'])}")
        return f"circular_pattern({', '.join(parts)})"
    if kind == "mirror":
        parts = [_lit(name)]
        if args.get("feature") is not None:
            parts.append(_expr(args["feature"]))
        parts.append(f"about={_bare(args.get('about', 'YZ'), _PLANES)}")
        return f"mirror({', '.join(parts)})"
    if kind == "import_step":
        parts = [_lit(name), _lit(args["path"])]
        if args.get("tolerance"):
            parts.append(f"tolerance={_lit(args['tolerance'])}")
        return f"import_step({', '.join(parts)})"
    if kind == "instance":
        parts = [_lit(name), _lit(args["path"])]
        for key in ("at", "rotate"):
            v = args.get(key)
            if v is not None and any(abs(float(c)) > 0 for c in v):
                parts.append(f"{key}={_tuple_text([float(c) for c in v], 4)}")
        for key in ("color", "material"):
            if args.get(key):
                parts.append(f"{key}={_lit(args[key])}")
        for key in ("density", "tolerance"):
            if args.get(key) is not None:
                parts.append(f"{key}={_lit(args[key])}")
        return f"instance({', '.join(parts)})"
    if kind == "fixed":
        return f"fixed({_lit(name)}, {_expr(args['instance'])})"
    if kind == "view":
        parts = [_lit(name)]
        section = args.get("section")
        if section is None:
            parts.append(f"direction={str(args.get('direction') or 'front').upper()}")
        parts.append(f"at={_tuple_text([float(c) for c in args.get('at', (0, 0))], 2)}")
        if args.get("scale") is not None:
            parts.append(f"scale={_lit(args['scale'])}")
        if args.get("hidden") is not None:
            parts.append(f"hidden={_lit(bool(args['hidden']))}")
        if section is not None:
            parts.append(f"section={_bare(section, _PLANES)}")
            if args.get("offset"):
                parts.append(f"offset={_lit(args['offset'])}")
            if args.get("flip"):
                parts.append("flip=True")
        return f"view({', '.join(parts)})"
    if kind == "dimension":
        parts = [_lit(name), _expr(args["a"])]
        if args.get("b") is not None:
            parts.append(_expr(args["b"]))
        parts.append(f"at={_tuple_text([float(c) for c in args.get('at', (0, 0))], 2)}")
        for key in ("kind", "along", "text"):
            if args.get(key):
                parts.append(f"{key}={_lit(args[key])}")
        return f"dimension({', '.join(parts)})"
    if kind == "note":
        parts = [_lit(name), _lit(str(args["text"])), f"at={_tuple_text([float(c) for c in args.get('at', (0, 0))], 2)}"]
        if args.get("size") not in (None, 3.5):
            parts.append(f"size={_lit(args['size'])}")
        if args.get("view"):
            parts.append(f"view={_expr(args['view'])}")
        return f"note({', '.join(parts)})"
    if kind in MATE_KINDS:
        parts = [_lit(name), _expr(args["a"]), _expr(args["b"])]
        if kind in ("distance", "angle"):
            if args.get("value") is None:
                raise EditError(f"{kind} needs a value")
            parts.append(_lit(args["value"]))
        if args.get("flip"):
            parts.append("flip=True")
        return f"{kind}({', '.join(parts)})"
    raise EditError(f"cannot compose a {kind!r} feature")


def compose_entity(sketch_var: str, kind: str, name: str, args: dict[str, Any]) -> str:
    from .parse import POSITIONAL

    positional = POSITIONAL.get(kind)
    if positional is None:
        raise EditError(f"cannot compose a {kind!r} entity")
    parts = [_lit(name)]
    for key in positional[1:]:
        if key not in args:
            raise EditError(f"{kind} needs {key}")
        parts.append(_lit(args[key]))
    for key, value in args.items():
        if key in positional or value is None:
            continue
        if key == "at" and tuple(value) == (0.0, 0.0):
            continue
        if is_default(kind, key, _lit(value)):
            continue
        parts.append(f"{key}={_lit(value)}")
    return f"{sketch_var}.{kind}({', '.join(parts)})"


def compose_constraint(sketch_var: str, kind: str, name: str, refs: list[str], value: Any = None,
                       options: dict[str, Any] | None = None) -> str:
    from .parse import CONSTRAINT_KINDS as KINDS
    from .parse import DIMENSION_KINDS

    if kind not in KINDS:
        raise EditError(f"unknown constraint kind {kind!r}")
    parts = [_lit(name), *(_lit(r) for r in refs)]
    if kind in DIMENSION_KINDS:
        if value is None:
            raise EditError(f"{kind} needs a value")
        parts.append(_lit(value))
    for key, val in (options or {}).items():
        if val not in (None, False):
            parts.append(f"{key}={_lit(val)}")
    return f"{sketch_var}.{kind}({', '.join(parts)})"


# --- JSON operations -----------------------------------------------------------

def apply(src: str, op: dict[str, Any]) -> str:
    """Apply one JSON edit operation and return the new source."""
    from .operations import validate_operation

    op = validate_operation(op)
    kind = op.get("op")
    try:
        if kind == "set_argument":
            return set_argument(src, op["feature"], op["kwarg"], _lit(op["value"]))
        if kind == "set_parameter":
            return set_parameter(src, op["name"], _lit(op["value"]))
        if kind == "add_parameter":
            return add_parameter(src, str(op["name"]), _lit(op["value"]), op.get("description"))
        if kind == "delete_parameter":
            return delete_parameter(src, str(op["name"]))
        if kind == "set_meta":
            return set_meta(src, str(op["key"]), _lit(op["value"]))
        if kind == "add_feature":
            stmt = op.get("statement") or compose_feature(op["kind"], op["name"], op.get("args", {}))
            after = op.get("after")
            return add_feature(src, after, stmt) if after else append_statement(src, stmt)
        if kind == "delete_feature":
            return delete_feature(src, op["feature"], bool(op.get("cascade", True)))
        if kind == "add_sketch_entity":
            sketch_name = op["sketch"]
            stmt = op.get("statement")
            if stmt is None:
                module = cst.parse_module(src)
                var = module.body[_feature_index(module, sketch_name, kind="sketch")].body[0].targets[0].target.value
                stmt = compose_entity(var, op["kind"], op["name"], op.get("args", {}))
            return add_sketch_entity(src, sketch_name, stmt)
        if kind == "delete_sketch_entity":
            return delete_sketch_entity(src, op["sketch"], op["entity"])
        if kind == "add_constraint":
            module = cst.parse_module(src)
            var = _sketch_var(module, op["sketch"])
            stmt = op.get("statement") or compose_constraint(var, op["kind"], op["name"], op.get("refs", []),
                                                             op.get("value"), op.get("options"))
            return add_sketch_entity(src, op["sketch"], stmt)
        if kind == "delete_constraint":
            return delete_sketch_entity(src, op["sketch"], op["constraint"])
        if kind == "set_constraint_value":
            return set_entity_argument(src, op["sketch"], op["constraint"], "value", _lit(op["value"]))
        if kind == "set_constraint_argument":
            return set_entity_argument(src, op["sketch"], op["constraint"], op["kwarg"], _lit(op["value"]))
        if kind == "write_back":
            return write_back(src, op["sketch"], op["coords"], int(op.get("precision", 4)))
        if kind == "write_poses":
            return write_poses(src, op["poses"], int(op.get("precision", 4)))
        if kind == "set_entity_argument":
            return set_entity_argument(src, op["sketch"], op["entity"], op["kwarg"], _lit(op["value"]))
        if kind == "batch":
            out = src
            for sub in op.get("ops", []):
                out = apply(out, sub)
            return out
        if kind == "replace_source":
            text = str(op["source"])
            if not text.strip():
                raise EditError("refusing to replace the file with nothing; delete the file if that is what you want")
            return text
    except KeyError as exc:
        raise EditError(f"operation {kind!r} is missing {exc}") from None
    except cst.ParserSyntaxError as exc:
        raise EditError(f"invalid expression in operation: {exc.message}") from None
    raise EditError(f"unknown operation {kind!r}")


def unified_diff(old: str, new: str, name: str = "model.py") -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}",
    ))
