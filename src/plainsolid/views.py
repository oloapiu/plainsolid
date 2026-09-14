"""View state sidecar: camera, named views, section, pins, visibility. Lives
next to the document as <name>.views.json, git-tracked, never required."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = 1


def default() -> dict[str, Any]:
    return {"version": VERSION, "camera": None, "section": None, "pins": [], "visibility": {},
            "transparency": {}, "named": {}, "dims": {}}


def sidecar_path(doc_path: str | Path) -> Path:
    p = Path(doc_path)
    return p.with_name(p.stem + ".views.json")


def load(doc_path: str | Path) -> dict[str, Any]:
    path = sidecar_path(doc_path)
    data = default()
    if not path.exists():
        return data
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data.update({k: v for k, v in loaded.items() if k in data})
    except (OSError, ValueError):
        data["warning"] = f"{path.name} is unreadable and was ignored"
    return data


def save(doc_path: str | Path, data: dict[str, Any]) -> dict[str, Any]:
    clean = default()
    clean.update({k: v for k, v in (data or {}).items() if k in clean})
    clean["version"] = VERSION
    sidecar_path(doc_path).write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return clean
