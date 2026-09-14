import pytest

from plainsolid import views

pytestmark = pytest.mark.unit


def test_sidecar_round_trip_and_malformed(tmp_path):
    doc = tmp_path / "part.py"
    doc.write_text("")
    assert views.load(doc)["named"] == {} and not views.sidecar_path(doc).exists()
    saved = views.save(doc, {"camera": {"pos": [1, 2, 3]}, "named": {"a": {"camera": None}}, "junk": 1})
    assert "junk" not in saved and views.sidecar_path(doc).name == "part.views.json"
    assert views.load(doc)["camera"] == {"pos": [1, 2, 3]}
    views.sidecar_path(doc).write_text("{nope")
    data = views.load(doc)
    assert data["camera"] is None and "ignored" in data["warning"]
