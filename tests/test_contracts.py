"""Request and generated-source contracts, without geometry fixtures."""

import ast
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plainsolid.edit import EditError, apply
from plainsolid.literals import python_literal
from plainsolid.operations import validate_operation
from plainsolid.parse import parse_document
from plainsolid.server import create_app
from plainsolid.workspace import template_source, wrapper_source

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text", ['Sam "Ace" O\'Neil', "line\nnext\t\\path", "Café 🛠", "\x00\b\r", "class"]
)
def test_generated_strings_round_trip(text):
    assert ast.literal_eval(python_literal(text)) == text
    for kind in ("part", "assembly", "drawing"):
        source = template_source(text, kind=kind, author=text, material=text, of=text)
        compile(source, "<template>", "exec")
        doc = parse_document(source)
        assert not doc.errors
        assert doc.meta["name"] == text
        assert doc.meta["author"] == text
        if kind == "drawing":
            assert doc.meta["of"] == text
        elif kind == "part":
            assert doc.meta["material"] == text
    for kind in ("part", "assembly"):
        source = wrapper_source(text + ".step", kind)
        compile(source, "<wrapper>", "exec")
        doc = parse_document(source)
        assert not doc.errors
        assert doc.meta["name"] == text
        assert doc.features[0].args["path"] == text + ".step"


def test_literal_tuples_and_edit_strings():
    assert ast.literal_eval(python_literal((7,))) == (7,)
    text = 'Café 🛠 "bracket"'
    doc = parse_document(
        apply(template_source("part", author=""), {"op": "set_meta", "key": "name", "value": text})
    )
    assert doc.meta["name"] == text
    for value in (float("nan"), float("inf"), {"expr": 4}):
        with pytest.raises(ValueError):
            python_literal(value)


INVALID_OPS = [
    ({"op": "missing"}, "op"),
    ({"op": "set_parameter", "name": "x", "value": {"expr": 4}}, "expr string"),
    ({"op": "set_parameter", "name": "x"}, "value"),
    ({"op": "delete_feature", "feature": "body", "cascade": "false"}, "cascade"),
    ({"op": "set_parameter", "name": 123, "value": 4}, "name"),
    ({"op": "add_feature", "kind": "sketch"}, "kind and name"),
    ({"op": "add_feature", "kind": "sketch", "name": "s", "args": []}, "args"),
    ({"op": "set_parameter", "name": "x", "value": 2, "typo": True}, "typo"),
    ({"op": "make_editable"}, "exactly one"),
    ({"op": "make_editable", "leaf": "a", "instance": "b"}, "exactly one"),
    ({"op": "write_poses", "poses": {"a": {"at": [1, 2], "rotate": [0, 0, 0]}}}, "at"),
    ({"op": "solve_sketch", "sketch": "s", "drag": {"a": [1]}}, "drag"),
    (
        {
            "op": "batch",
            "ops": [{"op": "set_meta", "key": "name", "value": "first"}, {"op": "set_parameter"}],
        },
        "ops",
    ),
    ({"op": "batch", "ops": [{"op": "make_editable", "leaf": "a"}]}, "separate operations"),
]


@pytest.mark.parametrize("operation,detail", INVALID_OPS)
def test_invalid_operations_are_rejected_before_writing(tmp_path, operation, detail):
    path = tmp_path / "model.py"
    original = template_source("original", author="")
    path.write_text(original)
    app = create_app(tmp_path, serve_client=False)
    doc = app.state.workspace.open(path)
    with pytest.raises(EditError, match=detail):
        app.state.workspace.apply(doc, operation, doc.hash)
    with TestClient(app) as client:
        for route, body in [
            ("edit", operation),
            ("preview", {"op": operation}),
            ("preview-mesh", {"op": operation}),
        ]:
            response = client.post(f"/api/documents/{doc.id}/{route}", json=body)
            assert response.status_code == 422, response.text
            assert detail in response.json()["error"]
            assert path.read_text() == original
            assert doc.source == original and not doc.undo


@pytest.mark.parametrize("body", [None, [], "set_parameter", 4])
def test_non_object_edits_have_readable_errors(tmp_path, body):
    app = create_app(tmp_path, serve_client=False)
    with TestClient(app) as client:
        response = client.post("/api/documents/unused/edit", json=body)
        assert response.status_code == 422
        assert isinstance(response.json()["error"], str)


def test_existing_statement_batch_and_tuple_clients_remain_supported():
    source = template_source("part", author="")
    op = {
        "op": "batch",
        "sketch": "s",
        "ops": [
            {"op": "add_feature", "statement": 's = sketch("s", on=XY)\n'},
            {
                "op": "add_sketch_entity",
                "sketch": "s",
                "kind": "point",
                "name": "p",
                "args": {"at": (1, 2)},
            },
        ],
    }
    doc = parse_document(apply(source, op))
    assert not doc.errors and doc.features[0].entities[0].name == "p"
    assert validate_operation(
        {"op": "write_poses", "poses": {"a": {"at": (0, 0, 0), "rotate": (0, 0, 0)}}}
    )


def test_generated_client_types_match_server_contract():
    script = Path(__file__).parents[1] / "scripts/generate_edit_types.py"
    spec = importlib.util.spec_from_file_location("edit_types", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TARGET.read_text() == module.generated()


def test_openapi_describes_edit_variants(tmp_path):
    schema = create_app(tmp_path, serve_client=False).openapi()
    body = schema["paths"]["/api/documents/{doc_id}/edit"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert body["discriminator"]["propertyName"] == "op"
    assert "batch" in body["discriminator"]["mapping"]
