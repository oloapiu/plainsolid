"""L0, L5, L6: suggestions, against a scripted model: the reply parsing, the checks and the
guards on the bracket, the propose-check-retry loop, the API and the CLI. No model runs."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from plainsolid import suggest as sg
from plainsolid.cli import app as cli_app
from plainsolid.server import create_app
from plainsolid.workspace import CACHE_DIR, Workspace, compact_tree

SPARK = sg.Settings(base_url="http://model.test/v1", model="m", api="llamacpp", reasoning="low", reasoning_budget=500)
FILLET = {"op": "add_feature", "kind": "fillet", "name": "wall_round", "after": "slot_cut",
          "args": {"edges": {"expr": 'body.edges.from_sketch("top").from_sketch("outer_wall")'}, "radius": 1}}


class Script:
    """A model that answers from a list, recording each request's body."""

    def __init__(self, *replies: str | tuple[str, str]):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.replies.pop(0) if self.replies else "{}"
        content, finish = reply if isinstance(reply, tuple) else (reply, "stop")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}],
                                         "usage": {"completion_tokens": 7, "prompt_tokens": 100}})

    def model(self, settings: sg.Settings = SPARK) -> sg.Model:
        return sg.Model(settings, httpx.MockTransport(self))


def proposal(op: dict, label: str = "a label") -> str:
    return json.dumps({"label": label, "op": op})


@pytest.fixture
def bracket(project: Path):
    ws = Workspace(project)
    return ws, ws.open("bracket.py")


def ask(model: sg.Model, ws, doc, hint: str, context: dict | None = None, **kw) -> sg.Suggestion:
    return sg.suggest(model, hint=hint, context=context or {}, path_name=doc.path.name, source=doc.source,
                      file_hash=doc.hash, tree=compact_tree(doc.tree_json(), doc),
                      trial=lambda op: ws.trial(doc, op), **kw)


def judge(ws, doc, op, hint: str = "", context: dict | None = None) -> sg.Verdict:
    return sg.judge(op, hint, context or {}, doc.source, lambda o: ws.trial(doc, o), doc.path.name)


# ---- settings and replies ------------------------------------------------------------------------

PROFILES = '''default = "flash"

[profiles.spark]
base_url = "http://spark:8000/v1"
model = "qwen3.6-35b-a3b-q8"
api = "llamacpp"
key_env = "TEST_SPARK_KEY"
reasoning = "low"
reasoning_budget = 1000

[profiles.flash]
base_url = "https://openrouter.ai/api/v1"
model = "qwen/qwen3.8-flash"
api = "openrouter"
key_env = "TEST_ROUTER_KEY"
'''


@pytest.mark.unit
def test_config_files(tmp_path: Path):
    assert sg.load_config(tmp_path / "none.toml") is None
    f = tmp_path / "suggest.toml"
    f.write_text('base_url = "http://x/v1"\nmodel = "m"\napi = "llamacpp"\nreasoning = "low"\nreasoning_budget = 1000\n')
    c = sg.load_config(f)                                                    # one model at the top level
    assert c.default == "default" and list(c.profiles) == ["default"]
    s = c.profiles["default"]
    assert (s.api, s.reasoning, s.reasoning_budget, s.retries) == ("llamacpp", "low", 1000, 1)
    f.write_text(PROFILES)
    c = sg.load_config(f)
    assert c.default == "flash" and list(c.profiles) == ["spark", "flash"] and c.profiles["flash"].api == "openrouter"
    f.write_text(PROFILES.replace('default = "flash"\n', ""))
    assert sg.load_config(f).default == "spark"                              # the first, without a default
    for bad in ('model = "m"\n', 'base_url = "u"\nmodel = "m"\napi = "grpc"\n',
                'base_url = "u"\nmodel = "m"\nreasoning = "lots"\n', 'base_url = "u"\nmodel = "m"\ncolour = 1\n',
                "not toml [",
                PROFILES.replace('default = "flash"', 'default = "luna"'),       # not a profile
                'default = "x"\nprofiles = {}\n',                                # no profiles
                'timeout = 5\n' + PROFILES,                                      # a stray top-level setting
                PROFILES.replace('reasoning = "low"', "tone = 1")):             # an unknown profile setting
        f.write_text(bad)
        with pytest.raises(sg.SuggestError):
            sg.load_config(f)


@pytest.mark.unit
def test_a_profile_without_its_key_is_not_ready_and_no_key_is_shown(tmp_path: Path, bracket,
                                                                    monkeypatch: pytest.MonkeyPatch):
    ws, doc = bracket
    f = tmp_path / "suggest.toml"
    f.write_text(PROFILES)
    monkeypatch.delenv("TEST_ROUTER_KEY", raising=False)
    monkeypatch.setenv("TEST_SPARK_KEY", "sk-very-secret")
    service = sg.Service(ws, loader=lambda: sg.load_config(f))
    status = service.status()
    by = {p["name"]: p for p in status["profiles"]}
    assert status["enabled"] and status["default"] == "flash"
    assert by["spark"]["ready"] and by["spark"]["problem"] is None
    assert not by["flash"]["ready"] and "TEST_ROUTER_KEY is not set" in by["flash"]["problem"]
    assert "sk-very-secret" not in json.dumps(status)
    with pytest.raises(sg.SuggestError, match="TEST_ROUTER_KEY"):
        service.suggest(doc, "round it")                                     # the default, not ready
    with pytest.raises(sg.SuggestError, match="no profile 'nope'"):
        service.suggest(doc, "round it", profile="nope")
    monkeypatch.delenv("TEST_SPARK_KEY")
    assert service.status()["enabled"] is False


@pytest.mark.unit
def test_parse_reply_forms():
    op = {"op": "set_parameter", "name": "thickness", "value": 5}
    assert sg.parse_reply(proposal(op, "thicker")) == ("thicker", op)
    assert sg.parse_reply("Here:\n```json\n" + proposal(op) + "\n```") == ("a label", op)
    assert sg.parse_reply("sure " + proposal(op) + " done") == ("a label", op)
    assert sg.parse_reply(json.dumps({"label": "x", "op": json.dumps(op)}))[1] == op  # a double-encoded op
    assert sg.parse_reply(json.dumps(op)) == (None, op)                              # the op without the wrapper
    hashed = {"op": "batch", "hash": "abc", "ops": [{**op, "hash": "abc"}]}
    assert sg.parse_reply(proposal(hashed))[1] == {"op": "batch", "ops": [op]}       # the guide's hash is dropped
    for bad in ("no json here", "[1, 2]", '{"label": "x"}', '{"op": "{not json"}'):
        with pytest.raises(sg.BadReply):
            sg.parse_reply(bad)


@pytest.mark.unit
def test_hint_numbers():
    assert sg.hint_numbers("3mm fillet") == [3]
    assert sg.hint_numbers("M6 holes") == []
    assert sg.hint_numbers("M6 clearance holes, 6.4") == [6.4]
    assert sg.hint_numbers("a 10x10x3 block") == [10, 10, 3]
    assert sg.hint_numbers("round the corners, r3") == [3]
    assert sg.hint_numbers("make hole2 bigger") == []


# ---- the checks and guards -----------------------------------------------------------------------

@pytest.mark.unit
def test_judge_refuses_what_cannot_be_offered(bracket):
    ws, doc = bracket
    assert "not allowed" in judge(ws, doc, {"op": "replace_source", "source": "x = 1"}).error
    assert "changes nothing" in judge(ws, doc, {"op": "set_parameter", "name": "thickness", "value": 4.0}).error
    assert "no top-level feature" in judge(ws, doc, {"op": "delete_feature", "feature": "nope"}).error
    missing = {**FILLET, "args": {**FILLET["args"], "edges": {"expr": 'body.edges.from_sketch("nothing")'}}}
    assert "wall_round" in judge(ws, doc, missing).error                      # a feature that newly fails
    conflict = {"op": "add_constraint", "sketch": "profile", "kind": "length", "name": "w2", "refs": ["bottom"], "value": 30}
    assert "conflicting" in judge(ws, doc, conflict).error
    assert doc.source == (ws.root / "bracket.py").read_text()                  # nothing was written


@pytest.mark.unit
def test_judge_accepts_and_notes(bracket):
    ws, doc = bracket
    v = judge(ws, doc, FILLET, "round this, 1mm")
    assert v.ok and v.notes == [] and "+++ b/bracket.py" in v.diff and "wall_round" in v.source
    plane = {"op": "add_feature", "kind": "plane", "name": "p1", "args": {"base": "XY", "offset": 10}}
    assert judge(ws, doc, plane, "a plane at 10").notes == ["the geometry does not change"]
    swap = {"op": "delete_feature", "feature": "outer"}
    assert any("deletes outer" in n for n in judge(ws, doc, swap, "3mm fillet").notes)
    assert judge(ws, doc, swap, "remove the chamfer").notes == []              # asked for
    assert judge(ws, doc, swap, "less", {"selected_feature": "outer"}).notes == []  # selected
    bigger = {"op": "set_argument", "feature": "outer", "kwarg": "distance", "value": 3}
    assert any("hint says 7" in n for n in judge(ws, doc, bigger, "chamfer 7").notes)
    assert judge(ws, doc, bigger, "chamfer 3").notes == []


# ---- the loop ------------------------------------------------------------------------------------

@pytest.mark.unit
def test_loop_retries_with_the_verdict(bracket):
    ws, doc = bracket
    bad = {**FILLET, "args": {**FILLET["args"], "edges": {"expr": 'body.edges.from_sketch("nothing")'}}}
    script = Script("not json", proposal(bad), proposal(FILLET, "round it"))
    s = ask(script.model(sg.Settings(**{**SPARK.__dict__, "retries": 2})), ws, doc, "round this, 1mm")
    assert s.ok and s.label == "round it" and s.op == FILLET and s.hash == doc.hash
    assert [a.ok for a in s.attempts] == [False, False, True]
    feedback = [m["content"] for m in script.requests[-1]["messages"] if m["role"] == "user"]
    assert "not a JSON object" in feedback[1] and "matches nothing" in feedback[2]
    first = script.requests[0]
    assert first["chat_template_kwargs"] == {"enable_thinking": True} and first["thinking_budget_tokens"] == 500
    assert first["response_format"] == {"type": "json_object"} and first["max_tokens"] == 500 + sg.ANSWER_ROOM
    assert "bracket.py" in first["messages"][0]["content"] and "Hint: round this, 1mm" in first["messages"][1]["content"]


@pytest.mark.unit
def test_loop_gives_up_and_keeps_the_best_with_notes(bracket):
    ws, doc = bracket
    bad = {"op": "delete_feature", "feature": "nope"}
    s = ask(Script(proposal(bad), proposal(bad)).model(), ws, doc, "whatever")
    assert not s.ok and "no proposal passed" in s.error and len(s.attempts) == 2
    swap = {"op": "delete_feature", "feature": "outer"}
    s = ask(Script(proposal(swap), proposal(swap)).model(), ws, doc, "3mm fillet")
    assert s.ok and any("deletes outer" in n for n in s.notes)                 # offered, with the note
    s = ask(Script(proposal(swap), proposal(FILLET)).model(), ws, doc, "3mm fillet")
    assert s.op == FILLET and s.notes == []                                    # the clean second try wins


@pytest.mark.unit
def test_loop_answers_again_when_the_budget_cuts_the_reply(bracket):
    ws, doc = bracket
    script = Script(("", "length"), proposal(FILLET))
    s = ask(script.model(), ws, doc, "round this, 1mm")
    assert s.ok and s.attempts[0].cut
    assert [r["chat_template_kwargs"]["enable_thinking"] for r in script.requests] == [True, False]


@pytest.mark.unit
def test_loop_can_be_cancelled_and_needs_a_hint(bracket):
    ws, doc = bracket
    with pytest.raises(sg.Cancelled):
        ask(Script(proposal(FILLET)).model(), ws, doc, "round it", cancelled=lambda: True)
    assert ask(Script().model(), ws, doc, "   ").error == "the hint is empty"


@pytest.mark.unit
def test_hosted_reasoning_switches(bracket):
    ws, doc = bracket
    router = Script(proposal(FILLET))
    ask(router.model(sg.Settings("http://x/v1", "q", api="openrouter", reasoning="low", reasoning_budget=800)), ws, doc, "r1")
    assert router.requests[0]["reasoning"] == {"effort": "low"} and router.requests[0]["max_tokens"] == 1800
    openai = Script(proposal(FILLET))
    ask(openai.model(sg.Settings("http://x/v1", "g", api="openai")), ws, doc, "round it 1")
    assert openai.requests[0]["reasoning_effort"] == "none" and openai.requests[0]["max_tokens"] == 2000


@pytest.mark.unit
def test_service_status_journal_and_prewarm(bracket):
    ws, doc = bracket
    off = sg.Service(ws, loader=lambda: None)
    assert off.status()["enabled"] is False and not off.prewarm(doc)
    with pytest.raises(sg.SuggestError, match="not configured"):
        off.suggest(doc, "round it")
    script = Script(proposal(FILLET), "{}", "{}")
    on = sg.Service(ws, loader=lambda: SPARK, transport=httpx.MockTransport(script))
    assert on.status()["enabled"] and on.status()["profiles"] == [{"name": "default", **SPARK.describe()}]
    out = on.suggest(doc, "round this, 1mm", {"mode": "part"})
    assert out["ok"] and out["op"] == FILLET
    assert not on.prewarm(doc)                                     # the suggestion warmed this file state
    ws.apply(doc, {"op": "set_parameter", "name": "thickness", "value": 5}, None)
    assert on.prewarm(doc) and not on.prewarm(doc)                 # a new state warms once
    assert script.requests[-1]["max_tokens"] == 1 and doc.source in script.requests[-1]["messages"][0]["content"]
    lines = [json.loads(x) for x in (ws.root / CACHE_DIR / sg.JOURNAL).read_text().splitlines()]
    assert lines[-1]["event"] == "suggest" and lines[-1]["hint"] == "round this, 1mm" and lines[-1]["ok"]


# ---- the API and the CLI -------------------------------------------------------------------------

@pytest.mark.api
def test_api_suggest_accept_and_outcome(project: Path):
    app = create_app(project, serve_client=False)
    with TestClient(app) as c:
        service = app.state.suggestions
        service.loader = lambda: None
        assert c.get("/api/suggest").json()["enabled"] is False
        did = c.post("/api/documents/open", json={"path": "bracket.py"}).json()["id"]
        r = c.post(f"/api/documents/{did}/suggest", json={"hint": "round it"})
        assert r.status_code == 502 and "not configured" in r.json()["error"]
        assert c.post(f"/api/documents/{did}/suggest", json={}).status_code == 422
        script = Script(proposal(FILLET, "round the wall's top edge"))
        service.loader, service.transport = (lambda: SPARK), httpx.MockTransport(script)
        assert c.get("/api/suggest").json()["profiles"] == [{"name": "default", **SPARK.describe()}]
        s = c.post(f"/api/documents/{did}/suggest", json={"hint": "round this, 1mm", "context": {"mode": "part"}}).json()
        assert s["ok"] and s["label"] == "round the wall's top edge" and "wall_round" in s["diff"]
        assert "wall_round" not in (project / "bracket.py").read_text()      # a suggestion writes nothing
        done = c.post(f"/api/documents/{did}/edit", json={**s["op"], "hash": s["hash"]}).json()
        assert done["changed"] and "wall_round" in (project / "bracket.py").read_text()
        assert c.post("/api/suggest/outcome", json={"id": s["id"], "outcome": "accepted"}).json() == {"ok": True}
        events = [json.loads(x)["event"] for x in (project / CACHE_DIR / sg.JOURNAL).read_text().splitlines()]
        assert events == ["suggest", "outcome"]


@pytest.mark.cli
def test_cli_suggest_and_apply(project: Path, monkeypatch: pytest.MonkeyPatch):
    script = Script(proposal(FILLET), proposal(FILLET))
    real = sg.Service
    monkeypatch.setattr(sg, "Service", lambda ws: real(ws, loader=lambda: SPARK, transport=httpx.MockTransport(script)))
    runner = CliRunner()
    f = project / "bracket.py"
    edge = FILLET["args"]["edges"]["expr"]
    result = runner.invoke(cli_app, ["suggest", str(f), "round this, 1mm", "--select", edge])
    out = json.loads(result.stdout)
    assert result.exit_code == 0 and out["ok"] and out["notes"] == [] and "wall_round" not in f.read_text()
    assert json.dumps(edge) in script.requests[0]["messages"][1]["content"]
    result = runner.invoke(cli_app, ["suggest", str(f), "round this, 1mm", "--apply"])
    assert result.exit_code == 0 and json.loads(result.stdout)["applied"]["changed"] and "wall_round" in f.read_text()


@pytest.mark.unit
def test_a_request_waits_for_the_prewarm_of_the_same_file(bracket):
    import threading

    ws, doc = bracket
    gate, arrived = threading.Event(), []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        arrived.append(body["max_tokens"])
        if body["max_tokens"] == 1:
            gate.wait(5)                                   # the prewarm is slow
        return httpx.Response(200, json={"choices": [{"message": {"content": proposal(FILLET)}, "finish_reason": "stop"}]})

    service = sg.Service(ws, loader=lambda: SPARK, transport=httpx.MockTransport(handler))
    warm = threading.Thread(target=service.prewarm, args=(doc,))
    warm.start()
    while not arrived:
        threading.Event().wait(0.01)
    ask_thread = threading.Thread(target=lambda: arrived.append(service.suggest(doc, "round this, 1mm")["ok"]))
    ask_thread.start()
    threading.Event().wait(0.3)
    assert arrived == [1]                                  # the request is holding back
    gate.set()
    warm.join(5)
    ask_thread.join(5)
    assert arrived[0] == 1 and arrived[-1] is True and len(arrived) == 3


SKETCHY = '''from plainsolid import *

meta(name="sketchy")

sketch1 = sketch("sketch1", on=XY)
sketch1.circle("circle1", 42, at=(0, 0))
sketch1.rect("rect1", 31, 27, at=(-36.5, 0), construction=True)
'''


@pytest.fixture
def sketchy(tmp_path: Path):
    (tmp_path / "sketchy.py").write_text(SKETCHY)
    ws = Workspace(tmp_path)
    return ws, ws.open("sketchy.py")


@pytest.mark.unit
def test_sketch_selections_are_described(sketchy):
    _, doc = sketchy
    ctx = sg.describe_selection(doc.document, {"mode": "sketch", "sketch": "sketch1",
                                               "selection": ["origin", "circle1", "rect1.right", "x_axis", "ghost"]})
    said = {s["ref"]: s["is"] for s in ctx["selection"] if isinstance(s, dict)}
    assert "built-in origin" in said["origin"] and 'name it "origin" in refs' in said["origin"]
    assert said["circle1"].startswith("the circle circle1")
    assert said["rect1.right"].startswith("the right of rect rect1 (construction)")
    assert "x axis" in said["x_axis"] and "ghost" in ctx["selection"]          # unknown names pass through
    part = {"mode": "part", "selection": [{"expr": "body.faces.top"}]}
    assert sg.describe_selection(doc.document, part) is part


@pytest.mark.unit
def test_a_sketch_size_must_be_a_constraint(sketchy):
    ws, doc = sketchy
    drawn = {"op": "set_entity_argument", "sketch": "sketch1", "entity": "circle1", "kwarg": "diameter", "value": 10}
    v = judge(ws, doc, drawn, "make it 5 mm radius", {"mode": "sketch", "sketch": "sketch1", "selection": ["circle1"]})
    assert v.ok and any("only drawn" in n for n in v.notes)
    assert not any("does not use" in n for n in v.notes)                     # radius 5 is a diameter of 10
    held = {"op": "add_constraint", "sketch": "sketch1", "kind": "radius", "name": "r1", "refs": ["circle1"], "value": 5}
    v = judge(ws, doc, held, "make it 5 mm radius")
    assert v.ok and v.notes == [] and any("degrees of freedom" in i for i in v.info)


@pytest.mark.unit
def test_degrees_of_freedom_are_said_but_not_retried(sketchy):
    ws, doc = sketchy
    circle = {"op": "batch", "ops": [
        {"op": "add_sketch_entity", "sketch": "sketch1", "kind": "circle", "name": "c2", "args": {"diameter": 10, "at": [20, 0]}},
        {"op": "add_constraint", "sketch": "sketch1", "kind": "radius", "name": "r2", "refs": ["c2"], "value": 5}]}
    script = Script(proposal(circle))
    s = ask(script.model(), ws, doc, "another circle, radius 5", {"mode": "sketch", "sketch": "sketch1", "selection": []})
    assert s.ok and len(s.attempts) == 1 and len(script.requests) == 1
    assert s.notes == [] and any("degrees of freedom" in i for i in s.info)


@pytest.mark.unit
def test_a_selection_the_proposal_ignores_and_a_lone_parameter_are_said(bracket, sketchy):
    ws, doc = bracket
    v = judge(ws, doc, FILLET, "round this, 1mm", {"mode": "part", "selection": [{"expr": "body.faces.top"}]})
    assert any("does not use the selection (body.faces.top)" in n for n in v.notes)
    assert judge(ws, doc, FILLET, "round this, 1mm", {"mode": "part", "selection": [{"expr": FILLET["args"]["edges"]["expr"]}]}).notes == []
    lone = {"op": "add_parameter", "name": "r", "value": 5}
    assert "the geometry does not change" in judge(ws, doc, lone, "radius 5").notes
    ws2, doc2 = sketchy
    circle = {"op": "add_sketch_entity", "sketch": "sketch1", "kind": "circle", "name": "c2", "args": {"diameter": 10, "at": [0, 0]}}
    v = judge(ws2, doc2, circle, "a circle of radius 5 centered here", {"mode": "sketch", "sketch": "sketch1", "selection": ["origin"]})
    assert not any("does not use 5" in n or "hint says 5" in n for n in v.notes)       # radius 5 is its diameter 10
    assert any("only drawn" in n for n in v.notes) and any("does not use the selection (origin)" in n for n in v.notes)


@pytest.mark.unit
def test_each_request_goes_to_the_profile_it_names(tmp_path: Path, bracket, monkeypatch: pytest.MonkeyPatch):
    ws, doc = bracket
    f = tmp_path / "suggest.toml"
    f.write_text(PROFILES)
    monkeypatch.setenv("TEST_SPARK_KEY", "k1")
    monkeypatch.setenv("TEST_ROUTER_KEY", "k2")
    seen: list[tuple[str, str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.host, request.headers["authorization"], body["max_tokens"] == 1))
        return httpx.Response(200, json={"choices": [{"message": {"content": proposal(FILLET)}, "finish_reason": "stop"}]})

    service = sg.Service(ws, loader=lambda: sg.load_config(f), transport=httpx.MockTransport(handler))
    out = service.suggest(doc, "round this, 1mm")
    assert out["profile"] == "flash" and out["model"] == "qwen/qwen3.8-flash"
    assert seen[-1][:2] == ("openrouter.ai", "Bearer k2")
    out = service.suggest(doc, "round this, 1mm", profile="spark")
    assert out["profile"] == "spark" and seen[-1][:2] == ("spark", "Bearer k1")
    assert service.prewarm(doc, "flash") is False                            # warm for this file already
    ws.apply(doc, {"op": "set_parameter", "name": "thickness", "value": 5}, None)
    assert service.prewarm(doc, "flash") and seen[-1] == ("openrouter.ai", "Bearer k2", True)
    lines = [json.loads(x) for x in (ws.root / CACHE_DIR / sg.JOURNAL).read_text().splitlines()]
    assert [x.get("profile") for x in lines if x["event"] == "suggest"] == ["flash", "spark"]
