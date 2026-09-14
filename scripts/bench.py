"""Measure what the server does per request: cold open, mesh, warm edit, preview and memory on
the zoo, on a generated stress assembly and, when present, on the local corpus. The numbers back
docs/testing.md's performance rule (fail at thirty percent over the recorded baseline).

    uv run python scripts/bench.py                 # measure; compare with tests/perf_baseline.json
    uv run python scripts/bench.py --record        # rewrite the baseline (one laptop's numbers)
    uv run python scripts/bench.py --check         # exit 1 on a regression
    uv run python scripts/bench.py --instances 120 --no-corpus
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import resource
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ZOO = ROOT / "zoo"
BASELINE = ROOT / "tests" / "perf_baseline.json"
CORPUS = ROOT / "corpus" / "node_s_210929.py"
TOLERANCE = 0.30  # docs/testing.md: fail when a budget is exceeded by more than thirty percent
SLACK_S = 0.02  # timer noise on the fastest documents
MEMORY_GROWTH = 0.15  # a preview burst may not grow the process by more than this
METRICS = ("cold_s", "eval_warm_s", "edit_s", "preview_s", "mesh_s", "mesh_warm_s", "rss_mb")

# one representative edit per zoo document, for the warm edit and the previews
EDITS: dict[str, dict[str, Any] | None] = {
    "bracket.py": {"op": "set_parameter", "name": "thickness", "value": 5},
    "enclosure.py": {"op": "set_parameter", "name": "length", "value": 121},
    "lid.py": {"op": "set_parameter", "name": "thickness", "value": 3.5},
    "node.py": {"op": "set_parameter", "name": "tilt", "value": 35},
    "node_review.py": None,
    "bracket_dwg.py": None,
}


def rss_mb() -> float:
    """The process's peak resident size; growth of the peak during a burst is the leak signal."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1e3


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def cold_caches() -> None:
    from plainsolid import assembly, drawing, mesh

    assembly._PARTS.clear()
    drawing._MODELS.clear()
    mesh.clear_memo()


def stress_source(n: int) -> str:
    """N lids and brackets in a grid, the first one fixed: many instances of two products."""
    lines = ["from plainsolid import *", 'meta(kind="assembly", name="stress")', ""]
    cols = max(1, int(n ** 0.5))
    for i in range(n):
        part = "lid.py" if i % 2 == 0 else "bracket.py"
        x, y = (i % cols) * 160.0, (i // cols) * 120.0
        lines.append(f'i{i} = instance("i{i}", "{part}", at=({x}, {y}, 0), rotate=(0, 0, {(i * 15) % 360}))')
    lines.append('fixed("anchor", i0)')
    return "\n".join(lines) + "\n"


def _variant(edit: dict[str, Any], i: float) -> dict[str, Any]:
    value = edit.get("value")
    return {**edit, "value": value + i} if isinstance(value, (int, float)) and not isinstance(value, bool) else edit


def measure_document(ws, name: str, edit: dict[str, Any] | None, *, previews: int = 3) -> dict[str, float]:
    from plainsolid import mesh as pmesh

    cold_caches()
    doc, open_s = timed(lambda: ws.open(name))
    ev, eval_s = timed(doc.ensure_evaluated)
    items = ev.items()
    _, mesh_s = timed(lambda: pmesh.build(items, None))
    _, mesh_warm_s = timed(lambda: pmesh.build(items, None))
    doc.evaluation = None
    _, eval_warm_s = timed(doc.ensure_evaluated)
    out = {"cold_s": open_s + eval_s, "eval_warm_s": eval_warm_s, "mesh_s": mesh_s, "mesh_warm_s": mesh_warm_s}
    if edit is not None:
        _, edit_s = timed(lambda: ws.apply(doc, edit, doc.hash))
        _, edit_mesh_s = timed(doc.mesh)
        out["edit_s"] = edit_s + edit_mesh_s
        times = [timed(lambda i=i: ws.preview_mesh(doc, _variant(edit, 0.5 + i)))[1] for i in range(previews)]
        out["preview_s"] = statistics.median(times)
    out["rss_mb"] = rss_mb()
    return out


def memory_burst(ws, name: str, edit: dict[str, Any], n: int = 30) -> dict[str, float]:
    doc = ws.open(name)
    ws.preview_mesh(doc, edit)
    before = rss_mb()
    for i in range(n):
        ws.preview_mesh(doc, _variant(edit, i))
    after = rss_mb()
    return {"previews": n, "rss_before_mb": before, "rss_after_mb": after,
            "growth": (after - before) / before if before else 0.0}


def measure_corpus(path: Path, previews: int = 3) -> dict[str, float]:
    """The local proposal: read only (previews write nothing), never part of the baseline."""
    from plainsolid import mesh as pmesh
    from plainsolid.workspace import Workspace

    cold_caches()
    ws = Workspace(path.parent)
    doc, open_s = timed(lambda: ws.open(path.name))
    ev, eval_s = timed(doc.ensure_evaluated)
    items = ev.items()
    _, mesh_s = timed(lambda: pmesh.build(items, None))
    _, mesh_warm_s = timed(lambda: pmesh.build(items, None))
    doc.evaluation = None
    _, eval_warm_s = timed(doc.ensure_evaluated)
    out: dict[str, float] = {"cold_s": open_s + eval_s, "eval_warm_s": eval_warm_s, "mesh_s": mesh_s,
                             "mesh_warm_s": mesh_warm_s, "features": len(doc.document.features)}
    inst = next((f for f in doc.document.features if f.kind == "instance"), None)
    if inst is not None:
        op = {"op": "set_argument", "feature": inst.name, "kwarg": "at", "value": [0, 0, 1]}
        out["preview_s"] = statistics.median(timed(lambda: ws.preview_mesh(doc, op))[1] for _ in range(previews))
    out["rss_mb"] = rss_mb()
    return out


def measure_all(instances: int = 60, corpus: Path | None = None, previews: int = 3) -> dict[str, Any]:
    from plainsolid.workspace import Workspace

    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "zoo"
        shutil.copytree(ZOO, scratch, ignore=shutil.ignore_patterns(".plainsolid-cache"))
        ws = Workspace(scratch)
        for name, edit in EDITS.items():
            results[name] = measure_document(ws, name, edit, previews=previews)
        (scratch / "stress.py").write_text(stress_source(instances))
        move = {"op": "set_argument", "feature": "i1", "kwarg": "at", "value": [10, 0, 0]}
        results[f"stress_{instances}"] = measure_document(ws, "stress.py", move, previews=previews)
        results["memory"] = memory_burst(ws, "enclosure.py", EDITS["enclosure.py"])
    if corpus is not None and corpus.exists():
        results["corpus"] = measure_corpus(corpus, previews)
    return results


def regressions(results: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    out = []
    for name, metrics in baseline.get("results", {}).items():
        if name in ("memory", "corpus") or name not in results:
            continue
        for metric, base in metrics.items():
            got = results[name].get(metric)
            if metric == "rss_mb" or got is None:
                continue
            if got > base * (1 + TOLERANCE) + SLACK_S:
                out.append(f"{name} {metric}: {got:.3f} s vs baseline {base:.3f} s")
    mem = results.get("memory")
    if mem and mem["growth"] > MEMORY_GROWTH:
        out.append(f"memory: {mem['previews']} previews grew the process by {mem['growth'] * 100:.0f}%")
    return out


def report(results: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    lines = [f"{'document':16s}" + "".join(f"{m:>15s}" for m in METRICS)]
    for name, r in results.items():
        if name == "memory":
            continue
        row = f"{name:16s}"
        for m in METRICS:
            v = r.get(m)
            cell = "" if v is None else (f"{v:.0f}" if m == "rss_mb" else f"{v:.3f}")
            base = ((baseline or {}).get("results", {}).get(name) or {}).get(m)
            if base and v is not None and m != "rss_mb":
                cell += f" {(v / base - 1) * 100:+.0f}%"
            row += f"{cell:>15s}"
        lines.append(row)
    mem = results.get("memory")
    if mem:
        lines.append(f"{mem['previews']} previews on the enclosure: peak rss {mem['rss_before_mb']:.0f} -> "
                     f"{mem['rss_after_mb']:.0f} MB ({mem['growth'] * 100:+.1f}%)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record", action="store_true", help="write tests/perf_baseline.json")
    parser.add_argument("--check", action="store_true", help="exit 1 when a number regresses past the tolerance")
    parser.add_argument("--instances", type=int, default=None, help="size of the generated stress assembly")
    parser.add_argument("--no-corpus", action="store_true", help="skip the local corpus assembly")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    args = parser.parse_args()
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
    instances = args.instances or (baseline or {}).get("instances") or 60
    results = measure_all(instances, None if args.no_corpus else args.corpus)
    print(report(results, None if args.record else baseline))
    if args.record:
        kept = {k: v for k, v in results.items() if k != "corpus"}
        BASELINE.write_text(json.dumps({
            "machine": f"{platform.system()} {platform.machine()}", "python": platform.python_version(),
            "date": dt.datetime.now(tz=dt.UTC).date().isoformat(), "instances": instances, "results": kept,
        }, indent=1) + "\n")
        print(f"recorded {BASELINE.relative_to(ROOT)}")
        return 0
    if baseline is not None:
        slow = regressions(results, baseline)
        for line in slow:
            print("REGRESSION " + line)
        if args.check and slow:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
