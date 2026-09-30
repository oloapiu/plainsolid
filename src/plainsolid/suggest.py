"""Suggestions: one edit proposed from the GUI's context and a typed hint by a language model.

The model sees the agent guide, the document's source and compact tree, what the user has
selected and the hint, and answers with one edit operation. The engine checks the answer by
evaluating it, writing nothing, and gives the model the verdict when it fails: an operation
that does not apply, a feature that newly fails, conflicting constraints. Softer guards ask
for one more try and otherwise travel with the proposal as notes: the geometry did not
change, something the hint never mentioned would be deleted, a number from the hint is not
in the proposal. The person accepts, edits or dismisses it; nothing is written here.

The model is any OpenAI-compatible endpoint, configured in
`~/.config/plainsolid/suggest.toml` (or the file `PLAINSOLID_SUGGEST_CONFIG` names), as
named profiles the GUI offers in a picker:

    default = "spark"

    [profiles.spark]
    base_url = "http://spark:8000/v1"
    model = "qwen3.6-35b-a3b-q8"
    api = "llamacpp"               # llamacpp | openai | openrouter: how reasoning is switched
    key_env = "SPARK_API_KEY"      # the environment variable holding the key; never the key
    reasoning = "low"              # off | low | medium | high
    reasoning_budget = 1000        # reasoning tokens per call

One model's settings at the top level, without [profiles], work too. Without the file,
suggestions are off.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import edit as edit_ops
from . import query as pquery
from .evaluate import Evaluation
from .model import Document

GUIDE = Path(__file__).parent / "agent.md"
CONFIG_ENV = "PLAINSOLID_SUGGEST_CONFIG"
JOURNAL = "suggest-journal.jsonl"
APIS = ("llamacpp", "openai", "openrouter")
REASONING = ("off", "low", "medium", "high")
ANSWER_ROOM = 1000  # tokens left for the answer after a reasoning budget

# the operations a suggestion may use: edit.apply handles them alone, so the check and the
# accepted edit agree; whole-file rewrites and metadata stay with the person
ALLOWED_OPS = frozenset({
    "set_argument", "set_parameter", "add_parameter", "delete_parameter", "add_feature", "delete_feature",
    "add_sketch_entity", "delete_sketch_entity", "add_constraint", "delete_constraint",
    "set_constraint_value", "set_constraint_argument", "set_entity_argument", "batch",
})
DELETING_OPS = {"delete_feature": "feature", "delete_sketch_entity": "entity",
                "delete_constraint": "constraint", "delete_parameter": "name"}
# a hint that asks for something to go: deletions are then expected
REMOVAL_WORDS = re.compile(r"\b(remove|delete|drop|get rid|instead|replace|don'?t need|no longer|without|swap|turn)\b", re.IGNORECASE)

INSTRUCTIONS = """\
You are the suggestion engine inside plainsolid's GUI. The user is working on the document
below. They may have selected things in the viewport, the feature tree or a sketch, and they
typed a short hint. Propose the ONE edit operation (as the guide describes them; a `batch`
when it takes several steps) that does what the hint asks. Reply with a JSON object
{"label": "a few words for the user", "op": {...}} and nothing else. The proposal is
evaluated before the user sees it; if it fails you get the error and can propose again.

- Refer to selected faces and edges by the selection's `expr`, exactly as given.
- When the file has a parameter or a variable for a quantity, use it in an expression
  ({"expr": "name"}) rather than repeating the number; a number the hint gives is used as given.
- Change only what the hint asks. Do not delete or rewrite anything else.
- In a sketch, an entity's arguments (its size, `at`, its points) are only where it is drawn:
  the solver moves them. What the hint states (a size, a position, centred on something,
  tangent, level) is a constraint in the same batch (diameter, radius, length, distance,
  coincident, tangent, horizontal, ...), so it holds. Sketch references (entities, their
  handles like "circle1.center", and the built-ins "origin", "x_axis", "y_axis") are names in
  a constraint's `refs`, never Python expressions. A circle of diameter 8 on a selected point
  "p1.end", in sketch "s":
  {"op": "batch", "ops": [
    {"op": "add_sketch_entity", "sketch": "s", "kind": "circle", "name": "c1", "args": {"diameter": 8, "at": [x, y]}},
    {"op": "add_constraint", "sketch": "s", "kind": "coincident", "name": "c1_on_p1", "refs": ["c1.center", "p1.end"]},
    {"op": "add_constraint", "sketch": "s", "kind": "diameter", "name": "c1_d", "refs": ["c1"], "value": 8}]}
- What the user selected is what the hint is about: the proposal uses it.
- A new feature goes right after the feature it builds on (`after`) unless it belongs at the end.
"""


class SuggestError(Exception):
    """Suggestions are misconfigured, or the model could not be reached."""


class BadReply(Exception):
    """The model's reply is not a proposal; the message goes back to it."""


# ---- settings ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api: str = "openai"
    key_env: str | None = None
    reasoning: str = "off"
    reasoning_budget: int | None = None
    temperature: float = 0.2
    max_tokens: int = 2000       # per call without a reasoning budget
    timeout: float = 120.0
    retries: int = 1             # further proposals after a failed or flagged one

    @property
    def key(self) -> str | None:
        return os.environ.get(self.key_env) if self.key_env else None

    @property
    def problem(self) -> str | None:
        """Why this model cannot be asked, or None: its key variable is not set here."""
        if self.key_env and not os.environ.get(self.key_env):
            return f"{self.key_env} is not set in the environment the server runs in"
        return None

    def describe(self) -> dict[str, Any]:
        """What the GUI may show: never the key; the variable's name only when it is missing."""
        return {"model": self.model, "api": self.api, "reasoning": self.reasoning,
                "reasoning_budget": self.reasoning_budget, "ready": self.problem is None, "problem": self.problem}


def config_path() -> Path:
    if os.environ.get(CONFIG_ENV):
        return Path(os.environ[CONFIG_ENV]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "plainsolid" / "suggest.toml"


@dataclass(frozen=True)
class Config:
    """The configured models by name, and the one used when a request names none."""
    profiles: dict[str, Settings]
    default: str


def _settings(where: str, raw: dict[str, Any]) -> Settings:
    known = set(Settings.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise SuggestError(f"{where}: unknown settings {sorted(unknown)}; known: {sorted(known)}")
    if not raw.get("base_url") or not raw.get("model"):
        raise SuggestError(f"{where}: base_url and model are required")
    s = Settings(**raw)
    if s.api not in APIS:
        raise SuggestError(f"{where}: api is one of {', '.join(APIS)}, not {s.api!r}")
    if s.reasoning not in REASONING:
        raise SuggestError(f"{where}: reasoning is one of {', '.join(REASONING)}, not {s.reasoning!r}")
    return s


def load_config(path: Path | None = None) -> Config | None:
    """The configured models, or None when there is no configuration file. The file holds
    named `[profiles.NAME]` tables and a `default`; a file with one model's settings at the top
    level is one profile, "default". Keys are never in it: each profile names the environment
    variable that holds its key (`key_env`)."""
    path = path or config_path()
    if not path.is_file():
        return None
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SuggestError(f"{path}: {exc}") from None
    if "profiles" not in raw:
        return Config({"default": _settings(str(path), raw)}, "default")
    tables = raw.get("profiles")
    extra = set(raw) - {"profiles", "default"}
    if extra:
        raise SuggestError(f"{path}: with [profiles], the top level holds only `default`, not {sorted(extra)}")
    if not isinstance(tables, dict) or not tables or not all(isinstance(t, dict) for t in tables.values()):
        raise SuggestError(f"{path}: [profiles.NAME] tables are needed, at least one")
    profiles = {name: _settings(f"{path} [profiles.{name}]", t) for name, t in tables.items()}
    default = raw.get("default", next(iter(profiles)))
    if default not in profiles:
        raise SuggestError(f"{path}: default {default!r} is not a profile; profiles: {', '.join(profiles)}")
    return Config(profiles, default)


# ---- the model -----------------------------------------------------------------------------------

@dataclass
class Reply:
    content: str
    finish: str | None
    seconds: float
    output_tokens: int | None = None
    prompt_tokens: int | None = None
    cached_tokens: int | None = None


class Model:
    """An OpenAI-compatible chat endpoint, asked for one JSON object per call."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        headers = {"Authorization": f"Bearer {settings.key}"} if settings.key else {}
        self.client = httpx.Client(base_url=settings.base_url.rstrip("/"), headers=headers,
                                   timeout=settings.timeout, transport=transport)

    def complete(self, messages: list[dict[str, Any]], *, reasoning: bool = True,
                 max_tokens: int | None = None) -> Reply:
        s = self.settings
        on = reasoning and s.reasoning != "off"
        cap = s.reasoning_budget + ANSWER_ROOM if on and s.reasoning_budget else s.max_tokens
        body: dict[str, Any] = {"model": s.model, "messages": messages, "max_tokens": max_tokens or cap,
                                "temperature": s.temperature, "response_format": {"type": "json_object"}}
        if s.api == "llamacpp":  # the chat template knows on and off; the budget is native
            body["chat_template_kwargs"] = {"enable_thinking": on}
            if on and s.reasoning_budget:
                body["thinking_budget_tokens"] = s.reasoning_budget
        elif s.api == "openrouter":
            # a reasoning max_tokens there replaces the level and is spent in full, so the budget
            # caps the whole reply instead, and a reply cut short is asked again without reasoning
            body["reasoning"] = {"effort": s.reasoning} if on else {"enabled": False}
        else:
            body["reasoning_effort"] = s.reasoning if on else "none"
        t = time.perf_counter()
        try:
            r = self.client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise SuggestError(f"the model at {s.base_url} did not answer: {exc}") from None
        if r.status_code >= 400:
            raise SuggestError(f"the model answered {r.status_code}: {r.text[:300]}")
        d = r.json()
        choice = d["choices"][0]
        usage = d.get("usage") or {}
        timings = d.get("timings") or {}
        return Reply(content=choice["message"].get("content") or "", finish=choice.get("finish_reason"),
                     seconds=round(time.perf_counter() - t, 3),
                     output_tokens=timings.get("predicted_n", usage.get("completion_tokens")),
                     prompt_tokens=usage.get("prompt_tokens"),
                     cached_tokens=timings.get("cache_n", (usage.get("prompt_tokens_details") or {}).get("cached_tokens")))


# ---- the prompt ----------------------------------------------------------------------------------

def system_prompt(path_name: str, source: str, tree: dict[str, Any]) -> str:
    """Everything that does not change until the file does: cached by the endpoint."""
    return "\n\n".join([
        INSTRUCTIONS,
        "# The guide\n\n" + GUIDE.read_text(encoding="utf-8"),
        f"# The document: {path_name}\n\n```python\n{source}\n```",
        "# Its evaluated tree (compact)\n\n" + json.dumps(tree, separators=(",", ":")),
    ])


BUILTIN_REFS = {
    "origin": "the sketch's built-in origin, a fixed point at (0, 0)",
    "x_axis": "the sketch's built-in x axis, a fixed line through the origin",
    "y_axis": "the sketch's built-in y axis, a fixed line through the origin",
}


def describe_selection(document: Document, context: dict[str, Any]) -> dict[str, Any]:
    """In a sketch the GUI sends bare references ("origin", "circle1", "rect1.right"); say what
    each one is, and that it is named in a constraint's refs, so the model does not take a
    reference for a variable or a size for a constraint."""
    if context.get("mode") != "sketch" or not context.get("sketch"):
        return context
    feature = document.feature(str(context["sketch"]))
    entities = {e.name: e for e in feature.entities} if feature else {}
    described: list[Any] = []
    for item in context.get("selection") or []:
        ref = item if isinstance(item, str) else None
        name, _, handle = (ref or "").partition(".")
        if ref and name in BUILTIN_REFS and not handle:
            described.append({"ref": ref, "is": BUILTIN_REFS[name] + f'; name it "{ref}" in refs'})
        elif ref and name in entities:
            e = entities[name]
            what = f"the {handle} of {e.kind} {name}" if handle else f"the {e.kind} {name}"
            described.append({"ref": ref, "is": what + (" (construction)" if e.construction else "") + f'; name it "{ref}" in refs'})
        else:
            described.append(item)
    return {**context, "selection": described}


def user_prompt(hint: str, context: dict[str, Any]) -> str:
    return f"UI context:\n{json.dumps(context, indent=1)}\n\nHint: {hint}"


def parse_reply(text: str) -> tuple[str | None, dict[str, Any]]:
    """The label and operation from a reply: a JSON object, perhaps fenced or with text
    around it, perhaps with the op encoded as a string."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise BadReply("the reply is not a JSON object") from None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise BadReply(f"the reply is not a JSON object: {exc}") from None
    if not isinstance(data, dict):
        raise BadReply("the reply is not a JSON object")
    op = data.get("op")
    if isinstance(op, str) and not op.lstrip().startswith("{"):
        op, data = data, {}  # the operation itself, without the wrapper
    elif isinstance(op, str):
        try:
            op = json.loads(op)
        except json.JSONDecodeError:
            raise BadReply("`op` is a string that is not JSON") from None
    if not isinstance(op, dict):
        raise BadReply('the reply needs {"label": ..., "op": {...}}')
    label = data.get("label")
    return (str(label) if label else None), normalize_op(op)


def normalize_op(op: dict[str, Any]) -> dict[str, Any]:
    """The guide lets an operation carry the file hash; the edit path takes it off, and so
    does a suggestion, which is checked against the file as it is."""
    op = {k: v for k, v in op.items() if k != "hash"}
    if op.get("op") == "batch" and isinstance(op.get("ops"), list):
        op["ops"] = [normalize_op(o) if isinstance(o, dict) else o for o in op["ops"]]
    return op


# ---- checking ------------------------------------------------------------------------------------

@dataclass
class Verdict:
    ok: bool                                 # False: the proposal cannot be offered
    error: str | None = None                 # why not, for the model's next try
    notes: list[str] = field(default_factory=list)   # soft guards: one more try, else offered with these said
    info: list[str] = field(default_factory=list)    # facts about the result, shown but never retried
    source: str | None = None
    diff: str | None = None
    sketches: dict[str, Any] = field(default_factory=dict)


def _ops(op: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for o in op.get("ops", []) if isinstance(o, dict) for x in _ops(o)] if op.get("op") == "batch" else [op]


def _touched_sketches(op: dict[str, Any]) -> set[str]:
    names = {o["sketch"] for o in _ops(op) if isinstance(o.get("sketch"), str)}
    return names | ({op["sketch"]} if isinstance(op.get("sketch"), str) else set())


def _deleted(op: dict[str, Any], source: str) -> list[str]:
    """Names the operation removes, a cascade included."""
    out: list[str] = []
    for o in _ops(op):
        key = DELETING_OPS.get(o.get("op", ""))
        if not key or not isinstance(o.get(key), str):
            continue
        out.append(o[key])
        if o["op"] == "delete_feature" and o.get("cascade", True):
            with contextlib.suppress(Exception):  # an unknown feature fails the check anyway
                out.extend(edit_ops.dependents(source, o[key]).get("features", []))
    return list(dict.fromkeys(out))


def _mentioned(name: str, hint: str, context: dict[str, Any]) -> bool:
    """A name the hint or the selection speaks of: the name itself, a word of it, or a
    selection that refers to it."""
    text = hint.lower()
    words = {w for w in re.split(r"[_\W]+", name.lower()) if len(w) > 2}
    if name.lower() in text or any(re.search(rf"\b{re.escape(w)}", text) for w in words):
        return True
    blob = json.dumps(context).lower()
    return bool(re.search(rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])", blob))


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str):
        return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _numbers(v)]
    if isinstance(value, list):
        return [n for v in value for n in _numbers(v)]
    return []


def hint_numbers(hint: str) -> list[float]:
    """Numbers the hint gives as values: "3mm", "6.4", "10x10x3"; not those in names like M6."""
    out: list[float] = []
    for token in re.findall(r"[A-Za-z_]*\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)*[A-Za-z]*", hint):
        if re.match(r"[A-Za-z_]+\d", token) and not re.match(r"(?i)[rd]\d", token):
            continue  # a name or a thread size: M6, hole2, slot_1
        out.extend(float(n) for n in re.findall(r"\d+(?:\.\d+)?", token))
    return out


def _geometry(ev: Evaluation) -> dict[str, Any] | None:
    try:
        s = pquery.summary(ev)
        return {"volume": round(s["volume"], 6), "area": round(s["area"], 6), "counts": s["counts"],
                "center": [round(c, 6) for c in s.get("center_of_mass") or []]}
    except Exception:  # noqa: BLE001 - no body, a drawing: nothing to compare
        return None


def judge(op: Any, hint: str, context: dict[str, Any], source: str,
          trial: Callable[[dict[str, Any]], tuple[str, Document, Evaluation, Evaluation | None]],
          name: str = "model.py") -> Verdict:
    """Check a proposed operation. `trial` applies it in memory and evaluates (the
    workspace's `trial`); it raises EditError when the operation does not apply."""
    if not isinstance(op, dict):
        return Verdict(False, "the operation must be a JSON object")
    kinds = {o.get("op") for o in _ops(op)} | {op.get("op")}
    if not kinds <= ALLOWED_OPS:
        return Verdict(False, f"not allowed in a suggestion: {sorted(k or '?' for k in kinds - ALLOWED_OPS)}")
    try:
        new_source, parsed, base, ev = trial(op)
    except edit_ops.EditError as exc:
        return Verdict(False, str(exc))
    if new_source == source:
        return Verdict(False, "the operation changes nothing in the file")
    if parsed.errors or ev is None:
        return Verdict(False, "; ".join(f"line {e.line}: {e.message}" for e in parsed.errors) or "the file does not parse")
    failed_before = {r.name for r in base.results if not r.ok}
    problems = [f"{r.name}: {r.error.message if r.error else 'failed'}" for r in ev.results
                if not r.ok and r.name not in failed_before]
    sketches: dict[str, Any] = {}
    touched = _touched_sketches(op)
    for r in ev.results:
        if r.sketch is not None and r.name in touched:
            s = r.sketch
            sketches[r.name] = {"dof": s.dof, "conflicting": list(s.conflicting), "redundant": list(s.redundant)}
            if s.conflicting or s.redundant:
                problems.append(f"sketch {r.name}: conflicting {list(s.conflicting)}, redundant {list(s.redundant)}")
    diff = edit_ops.unified_diff(source, new_source, name)
    if problems:
        return Verdict(False, "; ".join(problems), source=new_source, diff=diff)
    notes: list[str] = []
    before, after = _geometry(base), _geometry(ev)
    # constraining a sketch may leave its geometry where it was drawn; anything else should change it
    if before is not None and before == after and not touched:
        notes.append("the geometry does not change")
    if not REMOVAL_WORDS.search(hint):
        unasked = [n for n in _deleted(op, source) if not _mentioned(n, hint, context)]
        if unasked:
            notes.append(f"deletes {', '.join(unasked)}, which the hint does not mention")
    source_numbers = set(_numbers(source))
    proposed = _numbers(op)
    # a radius and a diameter say the same size: 5 in the hint is met by a diameter of 10
    sized = bool(re.search(r'"(radius|diameter)"', json.dumps(op)))
    numbers = hint_numbers(hint)
    missing = [n for n in numbers if n not in source_numbers and not _meets(n, proposed, sized)]
    if missing:
        notes.append(f"the hint says {', '.join(f'{n:g}' for n in missing)}, which the proposal does not use")
    if touched:
        held = _numbers([o.get("value") for o in _ops(op) if o.get("op") in ("add_constraint", "set_constraint_value")])
        drawn = [n for n in numbers if _meets(n, proposed, sized) and not _meets(n, held, sized)]
        if drawn:
            notes.append(f"{', '.join(f'{n:g}' for n in drawn)} from the hint is only drawn, not a constraint, so the "
                         "solver can change it: add a dimension (diameter, radius, length, distance) with that value")
    selected = [(item if isinstance(item, str) else item.get("ref") or item.get("expr"))
                for item in context.get("selection") or []]
    selected = [r for r in selected if isinstance(r, str) and r]
    text = "\n".join(_strings(op))
    if selected and not any(r in text for r in selected):
        notes.append(f"the proposal does not use the selection ({', '.join(selected)}), which the hint is about")
    info = [f"sketch {name} keeps {st['dof']} degrees of freedom" for name, st in sketches.items() if st["dof"]]
    return Verdict(True, notes=notes, info=info, source=new_source, diff=diff, sketches=sketches)


def _strings(value: Any) -> list[str]:
    """Every string in an operation, as written: names, references, expressions."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _meets(n: float, values: list[float], sized: bool) -> bool:
    wanted = (n, 2 * n, n / 2) if sized else (n,)
    return any(abs(w - v) < 1e-9 for w in wanted for v in values)


# ---- one suggestion ------------------------------------------------------------------------------

@dataclass
class Attempt:
    label: str | None
    op: Any
    ok: bool
    error: str | None
    notes: list[str]
    seconds: float
    output_tokens: int | None
    cut: bool = False            # the reasoning ran past its budget and the reply was asked again


@dataclass
class Suggestion:
    ok: bool
    id: str
    hint: str
    hash: str                    # the file the proposal was checked against; the edit carries it
    label: str | None = None
    op: dict[str, Any] | None = None
    diff: str | None = None
    notes: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    sketches: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    seconds: float = 0.0
    attempts: list[Attempt] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Cancelled(Exception):
    """A newer request for the same document took over."""


def suggest(model: Model, *, hint: str, context: dict[str, Any], path_name: str, source: str, file_hash: str,
            tree: dict[str, Any], trial: Callable[[dict[str, Any]], Any], retries: int | None = None,
            cancelled: Callable[[], bool] = lambda: False) -> Suggestion:
    """Ask the model, check each answer, give it the verdict, and return the first proposal
    that passes every check; one with soft notes is returned when the tries run out."""
    hint = hint.strip()
    out = Suggestion(ok=False, id=uuid.uuid4().hex[:12], hint=hint, hash=file_hash)
    if not hint:
        out.error = "the hint is empty"
        return out
    retries = model.settings.retries if retries is None else retries
    messages = [{"role": "system", "content": system_prompt(path_name, source, tree)},
                {"role": "user", "content": user_prompt(hint, context)}]
    t0 = time.perf_counter()
    best: tuple[str | None, dict[str, Any], Verdict] | None = None
    for _ in range(retries + 1):
        if cancelled():
            raise Cancelled()
        reply = model.complete(messages)
        cut = reply.finish == "length" and model.settings.reasoning != "off"
        if cut:  # the reasoning ran past the budget without an answer: answer now, without it
            if cancelled():
                raise Cancelled()
            reply = model.complete(messages, reasoning=False)
        messages.append({"role": "assistant", "content": reply.content})
        try:
            label, op = parse_reply(reply.content)
        except BadReply as exc:
            out.attempts.append(Attempt(None, None, False, str(exc), [], reply.seconds, reply.output_tokens, cut))
            messages.append({"role": "user", "content": f"{exc}. Reply with the JSON object only."})
            continue
        if cancelled():
            raise Cancelled()
        verdict = judge(op, hint, context, source, trial, path_name)
        out.attempts.append(Attempt(label, op, verdict.ok, verdict.error, verdict.notes, reply.seconds,
                                    reply.output_tokens, cut))
        if verdict.ok and (best is None or len(verdict.notes) < len(best[2].notes)):
            best = (label, op, verdict)
        if verdict.ok and not verdict.notes:
            break
        feedback = verdict.error if not verdict.ok else "It applies, but: " + "; ".join(verdict.notes) + \
            ". If that is what the hint asks, propose it again unchanged; otherwise fix it."
        messages.append({"role": "user", "content": f"{feedback}\nPropose again."})
    out.seconds = round(time.perf_counter() - t0, 3)
    if best is None:
        last = out.attempts[-1] if out.attempts else None
        out.error = f"no proposal passed the checks: {last.error}" if last else "no proposal"
        return out
    label, op, verdict = best
    out.ok, out.label, out.op = True, label or op.get("op"), op
    out.diff, out.notes, out.info, out.sketches = verdict.diff, verdict.notes, verdict.info, verdict.sketches
    return out


def prewarm(model: Model, *, path_name: str, source: str, tree: dict[str, Any]) -> float:
    """Send the stable part of the prompt once, so the endpoint caches it before the hint
    arrives. Returns the seconds it took."""
    reply = model.complete([{"role": "system", "content": system_prompt(path_name, source, tree)},
                            {"role": "user", "content": "(warming up)"}], max_tokens=1)
    return reply.seconds


# ---- the journal ---------------------------------------------------------------------------------

_journal_lock = threading.Lock()


def journal(cache_dir: Path, record: dict[str, Any]) -> None:
    """Append one line to the project's suggestion journal (derived state, gitignored):
    hints with their context and proposals, and later what the person did with them."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}, default=str)
    with _journal_lock, (cache_dir / JOURNAL).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---- the service the server and the CLI share ----------------------------------------------------

class Service:
    """Suggestions for a workspace's documents. Kernel work (evaluating the tree, trying a
    proposal) goes through `kernel`, which the server points at its kernel thread; the model
    is called from the caller's thread so the kernel stays free while it thinks. A newer
    request for a document cancels the older one. Documents someone asked about are
    prewarmed again after each change, with the profile last used on them, so the next hint
    finds the prompt cached. The configuration is read on every call: edits to it apply at once."""

    def __init__(self, workspace: Any, kernel: Callable[[Callable[[], Any]], Any] = lambda fn: fn(),
                 loader: Callable[[], Config | Settings | None] = load_config,
                 transport: httpx.BaseTransport | None = None):
        self.ws = workspace
        self.kernel = kernel
        self.loader = loader
        self.transport = transport
        self._models: dict[str, Model] = {}
        self._lock = threading.Lock()
        self._latest: dict[str, str] = {}                  # doc id -> the request that counts
        self._profile: dict[str, str] = {}                 # doc id -> the profile last asked on it
        self._warm: dict[tuple[str, str], str | None] = {}  # (doc id, profile) -> the hash last prewarmed
        self._warming: dict[tuple[str, str], tuple[str, threading.Event]] = {}  # a prewarm in flight

    def config(self) -> Config | None:
        c = self.loader()
        return Config({"default": c}, "default") if isinstance(c, Settings) else c

    def status(self) -> dict[str, Any]:
        """The profiles and whether each can be asked; `enabled` when one can."""
        try:
            c = self.config()
        except SuggestError as exc:
            return {"enabled": False, "error": str(exc), "config": str(config_path()), "profiles": []}
        if c is None:
            return {"enabled": False, "config": str(config_path()), "profiles": []}
        profiles = [{"name": name, **s.describe()} for name, s in c.profiles.items()]
        return {"enabled": any(p["ready"] for p in profiles), "default": c.default, "config": str(config_path()),
                "profiles": profiles}

    def model(self, profile: str | None = None) -> tuple[str, Model]:
        """The named profile's model (the default's without a name), ready to be asked."""
        c = self.config()
        if c is None:
            raise SuggestError(f"suggestions are not configured: write {config_path()}")
        name = profile or c.default
        s = c.profiles.get(name)
        if s is None:
            raise SuggestError(f"no profile {name!r} in {config_path()}; profiles: {', '.join(c.profiles)}")
        if s.problem:
            raise SuggestError(f"profile {name}: {s.problem}")
        with self._lock:
            m = self._models.get(name)
            if m is None or m.settings != s:
                m = self._models[name] = Model(s, self.transport)
            return name, m

    def _snapshot(self, doc: Any, context: dict[str, Any] | None = None) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        from .workspace import compact_tree

        def read() -> tuple[str, str, dict[str, Any], dict[str, Any]]:
            with doc.lock:
                return (doc.source, doc.hash, compact_tree(doc.tree_json(), doc),
                        describe_selection(doc.document, context or {}))
        return self.kernel(read)

    def suggest(self, doc: Any, hint: str, context: dict[str, Any] | None = None,
                profile: str | None = None) -> dict[str, Any]:
        name, model = self.model(profile)
        request = uuid.uuid4().hex
        key = (doc.id, name)
        with self._lock:
            self._latest[doc.id] = request
            self._profile[doc.id] = name
            self._warm.setdefault(key, None)
        source, file_hash, tree, context = self._snapshot(doc, context)
        # a prewarm of this very file state is on its way: wait for it, and the request finds the
        # prompt cached, rather than racing it and paying for the whole prompt twice
        warming = self._warming.get(key)
        if warming and warming[0] == file_hash:
            warming[1].wait(timeout=model.settings.timeout)
        try:
            s = suggest(model, hint=hint, context=context, path_name=doc.path.name, source=source, file_hash=file_hash,
                        tree=tree, trial=lambda op: self.kernel(lambda: self.ws.trial(doc, op)),
                        cancelled=lambda: self._latest.get(doc.id) != request)
        except Cancelled:
            return {"ok": False, "cancelled": True, "error": "a newer hint took over"}
        with self._lock:
            self._warm[key] = file_hash
        out = {**s.to_json(), "profile": name, "model": model.settings.model}
        self.record({"event": "suggest", "id": s.id, "doc": self._rel(doc), "hash": file_hash, "hint": s.hint,
                     "context": context, "profile": name, "model": model.settings.describe(), "ok": s.ok, "label": s.label, "op": s.op,
                     "notes": s.notes, "info": s.info, "error": s.error, "seconds": s.seconds, "attempts": len(s.attempts)})
        return out

    def prewarm(self, doc: Any, profile: str | None = None, force: bool = False) -> bool:
        """Warm the profile's cache for this document; False when it already is, or when the
        profile cannot be asked."""
        try:
            name, model = self.model(profile)
        except SuggestError:
            return False
        key = (doc.id, name)
        with self._lock:
            self._profile[doc.id] = name
        source, file_hash, tree, _ = self._snapshot(doc)
        with self._lock:
            if not force and self._warm.get(key) == file_hash:
                return False
            self._warm[key] = file_hash
            done = threading.Event()
            self._warming[key] = (file_hash, done)
        try:
            prewarm(model, path_name=doc.path.name, source=source, tree=tree)
        except SuggestError:
            with self._lock:
                self._warm[key] = None
            return False
        finally:
            done.set()
            with self._lock:
                if self._warming.get(key, (None, None))[1] is done:
                    del self._warming[key]
        return True

    def changed(self, doc: Any) -> None:
        """A document changed: prewarm it in the background, with its last profile, if it is in use."""
        profile = self._profile.get(doc.id)
        if profile is not None:
            threading.Thread(target=self.prewarm, args=(doc, profile), daemon=True, name="suggest-prewarm").start()

    def record(self, entry: dict[str, Any]) -> None:
        from .workspace import CACHE_DIR

        with contextlib.suppress(OSError):  # the journal never fails a suggestion
            journal(self.ws.root / CACHE_DIR, entry)

    def _rel(self, doc: Any) -> str:
        try:
            return str(doc.path.relative_to(self.ws.root))
        except ValueError:
            return str(doc.path)
