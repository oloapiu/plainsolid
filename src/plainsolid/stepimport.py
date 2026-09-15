"""STEP import that keeps the assembly hierarchy, instance names, colours and
locations. Based on build123d's importer, with the component (instance) label
preferred over the product label so shared geometry keeps distinct names.
"""
from __future__ import annotations

import copy
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from build123d import Compound, Location, Shape, Solid
from build123d.topology import downcast
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Quantity import Quantity_Color, Quantity_ColorRGBA
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.XCAFDoc import (
    XCAFDoc_ColorCurv,
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorTool,
    XCAFDoc_DocumentTool,
)


class ImportError_(ValueError):
    pass


@dataclass
class Instance:
    """One node of an imported assembly. Leaves carry a shape in world coordinates."""

    name: str
    product: str
    path: str
    color: tuple[float, float, float, float] | None = None
    transform: list[list[float]] | None = None
    shape: Shape | None = None
    children: list[Instance] = field(default_factory=list)
    file: str | None = None  # the part or STEP file an instance feature loaded
    kind: str = "step"  # part | step
    local_shape: Shape | None = None  # the product's own geometry, before its location in the file
    node: str | None = None  # the node's path inside its STEP file, kept when a document renames the tree
    loc: Location | None = field(default=None, repr=False, compare=False)  # where the file places the node
    index: dict[str, Instance] | None = field(default=None, repr=False, compare=False)  # path -> node, built on demand

    @property
    def is_assembly(self) -> bool:
        return bool(self.children)

    def leaves(self) -> list[Instance]:
        if not self.children:
            return [self]
        out: list[Instance] = []
        for c in self.children:
            out.extend(c.leaves())
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "product": self.product, "path": self.path,
            "color": list(self.color) if self.color else None,
            "assembly": self.is_assembly,
            "solids": len(self.shape.solids()) if self.shape is not None else 0,
            "children": [c.to_json() for c in self.children],
            "transform": self.transform, "file": self.file, "kind": self.kind, "node": self.node,
        }


def _clean(name: str) -> str:
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip()
    return re.sub(r"\s+", "_", name)


def _label_name(label: TDF_Label) -> str:
    n = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), n):
        return _clean(TCollection_AsciiString(n.Get()).ToCString())
    return ""


def _srgb(col: Quantity_ColorRGBA) -> tuple[float, float, float, float]:
    """OCCT reads STEP colours as sRGB and stores them linear; give back sRGB for display."""
    rgb = col.GetRGB()
    to = Quantity_Color.Convert_LinearRGB_To_sRGB_s
    return (round(to(rgb.Red()), 4), round(to(rgb.Green()), 4), round(to(rgb.Blue()), 4), round(col.Alpha(), 4))


def _label_color(label: TDF_Label) -> tuple[float, float, float, float] | None:
    col = Quantity_ColorRGBA()
    for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv):
        if XCAFDoc_ColorTool.GetColor_s(label, kind, col):
            return _srgb(col)
    return None


def _shape_color(color_tool, shape) -> tuple[float, float, float, float] | None:
    """Colour of the shape itself, else of its largest coloured face."""
    col = Quantity_ColorRGBA()
    for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
        if color_tool.GetColor(shape, kind, col):
            return _srgb(col)
    best = None
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        face = explorer.Current()
        for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
            if color_tool.GetColor(face, kind, col):
                best = _srgb(col)
                break
        if best:
            break
        explorer.Next()
    return best


def _matrix(loc: Location) -> list[list[float]]:
    t = loc.wrapped.Transformation()
    return [[t.Value(r, c) for c in range(1, 5)] for r in range(1, 4)] + [[0.0, 0.0, 0.0, 1.0]]


def read_step(path: str | os.PathLike) -> Instance:
    """Parse a STEP file into an Instance tree (uncached)."""
    path = Path(path)
    if not path.exists():
        raise ImportError_(f"no such file: {path}")
    doc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    reader.SetLayerMode(True)
    status = reader.ReadFile(os.fsdecode(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ImportError_(f"could not read STEP file {path.name}")
    reader.Transfer(doc)

    used: set[str] = set()
    counter = [0]

    def unique(base: str) -> str:
        if not base:
            counter[0] += 1
            base = f"part_{counter[0]}"
        name, i = base, 1
        while name in used:
            i += 1
            name = f"{base}_{i}"
        used.add(name)
        return name

    def walk(parent: TDF_Label | None, parent_loc: Location, parent_path: str) -> list[Instance]:
        labels = TDF_LabelSequence()
        if parent is None:
            shape_tool.GetFreeShapes(labels)
        else:
            shape_tool.GetComponents_s(parent, labels)
        out: list[Instance] = []
        for i in range(1, labels.Length() + 1):
            lab = labels.Value(i)
            ref = TDF_Label()
            if shape_tool.IsReference_s(lab):
                shape_tool.GetReferredShape_s(lab, ref)
            else:
                ref = lab
            inst_name, prod_name = _label_name(lab), _label_name(ref)
            name = unique(inst_name or prod_name)
            loc = parent_loc * Location(shape_tool.GetLocation_s(lab))
            path_ = f"{parent_path}.{name}" if parent_path else name
            color = _label_color(lab) or _label_color(ref)
            inst = Instance(name=name, product=prod_name or inst_name, path=path_, color=color,
                            transform=_matrix(loc), node=path_, loc=loc)
            if shape_tool.IsAssembly_s(ref):
                inst.children = walk(ref, loc, path_)
            else:
                topo = downcast(shape_tool.GetShape_s(ref))
                if color is None:
                    inst.color = _shape_color(color_tool, topo)
                shape = _cast(topo)
                inst.local_shape = shape
                inst.shape = shape.moved(loc)
            out.append(inst)
        return out

    roots = walk(None, Location(), "")
    if not roots:
        raise ImportError_(f"{path.name} contains no shapes")
    if len(roots) == 1:
        return roots[0]
    root = Instance(name=unique(path.stem), product=path.stem, path=path.stem, transform=_matrix(Location()),
                    loc=Location())
    for r in roots:
        _reparent(r, root.path)
    root.children = roots
    root.node = root.path
    return root


def _reparent(inst: Instance, prefix: str) -> None:
    inst.path = inst.node = f"{prefix}.{inst.path}"
    for c in inst.children:
        _reparent(c, prefix)


def relocated(node: Instance) -> Instance:
    """A copy of a node's subtree in the node's own coordinates: the node at the
    origin, its parts where it places them. What `file.step#node` means."""
    if node.loc is None:
        return node
    inv = node.loc.inverse()

    def fix(n: Instance) -> Instance:
        c = copy.copy(n)
        c.index = None
        c.loc = inv * n.loc if n.loc is not None else None
        if c.loc is not None:
            c.transform = _matrix(c.loc)
        if n.local_shape is not None and c.loc is not None:
            c.shape = n.local_shape.moved(c.loc)
        elif n.shape is not None:
            c.shape = n.shape.moved(inv)
        c.children = [fix(ch) for ch in n.children]
        return c

    return fix(node)


def _cast(topo) -> Shape:
    from build123d.topology import Compound as C
    from build123d.topology import Edge, Face, Shell, Vertex, Wire
    from build123d.topology import Solid as S
    from OCP.TopoDS import (
        TopoDS_Compound,
        TopoDS_Edge,
        TopoDS_Face,
        TopoDS_Shell,
        TopoDS_Solid,
        TopoDS_Vertex,
        TopoDS_Wire,
    )
    lut = {TopoDS_Compound: C, TopoDS_Solid: S, TopoDS_Shell: Shell, TopoDS_Face: Face,
           TopoDS_Wire: Wire, TopoDS_Edge: Edge, TopoDS_Vertex: Vertex}
    return lut.get(type(topo), Compound)(topo)


# --- cache -----------------------------------------------------------------

_CACHE: dict[tuple[str, int, int], Instance] = {}
_CACHE_MAX = 8


def file_key(path: str | os.PathLike) -> tuple[str, int, int]:
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)


def import_step_tree(path: str | os.PathLike) -> Instance:
    """Cached by path, mtime and size: re-evaluating a document does not re-read the file."""
    key = file_key(path)
    tree = _CACHE.get(key)
    if tree is None:
        tree = read_step(path)
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = tree
    return tree


def single_solid(tree: Instance, local: bool = False) -> Solid:
    """The one solid of an import used as a part body, or an error. `local` takes
    the product's own geometry rather than where the file places it."""
    solids = []
    for leaf in tree.leaves():
        shape = leaf.local_shape if local and leaf.local_shape is not None else leaf.shape
        if shape is not None:
            solids.extend(shape.solids())
    if len(solids) != 1:
        raise ImportError_(
            f"the STEP file holds {len(solids)} solids; a part is one solid body. "
            "Open it as an assembly: meta(kind=\"assembly\")"
        )
    return solids[0]


def split_fragment(text: str) -> tuple[str, str | None]:
    """`"vendor/node.step#node.glands.gland_1"` -> the file and the path of one node inside it."""
    file, _, fragment = str(text).partition("#")
    return file, (fragment or None)


def select_node(tree: Instance, fragment: str) -> Instance:
    """The node of an imported tree named by a fragment: its full path, a path
    below the root, or a unique node name."""
    if tree.index is None:
        nodes: list[Instance] = []

        def walk(n: Instance) -> None:
            nodes.append(n)
            for c in n.children:
                walk(c)
        walk(tree)
        tree.index = {n.path: n for n in nodes}
    exact = tree.index.get(fragment)
    if exact is not None:
        return exact
    nodes = list(tree.index.values())
    hits = [n for n in nodes if n.path.endswith("." + fragment)]
    if not hits:
        hits = [n for n in nodes if n.name == fragment]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sample = ", ".join(n.path for n in tree.leaves()[:6])
        raise ImportError_(f"no node {fragment!r} in {tree.name}; leaves are {sample}" + (", ..." if len(tree.leaves()) > 6 else ""))
    raise ImportError_(f"{fragment!r} names {len(hits)} nodes; use a longer path such as {hits[0].path!r}")
