# plainsolid for agents

plainsolid is a parametric CAD engine. A model is a Python file that the engine
executes into a feature tree and evaluates into geometry with build123d. The
file is the only truth: every tool edits it with a surgical patch, re-evaluates
it, and reports what happened. Units are millimetres and degrees. Nothing else
persists: no hidden model state, no binary files.

Three document kinds share the same syntax: a **part** (one solid body), an
**assembly** (instances of part files and vendor STEP files, held by mates) and
a **drawing** (views of a part or assembly with dimensions and notes).

## Two ways in

**The CLI**, one process per command, in the project directory:

```
plainsolid new FILE [--kind assembly | --kind drawing --of MODEL] [--name N] [--material M]
plainsolid tree FILE                     features, parameters, results, errors (JSON)
plainsolid edit FILE 'OP_JSON' [--dry-run]   one edit operation, or @ops.json
plainsolid query FILE [summary|volume|area|bbox|counts|center_of_mass|mass|bom|interference] [--upto F]
plainsolid measure FILE REF [REF]        a selector, face:ID, edge:ID, vertex:ID or point:X,Y,Z
plainsolid render FILE -o out.png [--view iso|front|back|top|bottom|left|right] [--size 800x600]
                  [--highlight SELECTOR]... [--upto F] [--plane XY --offset D] [--overlay OTHER | --rev HEAD]
                  the answer's caption says what each highlighted entity is and whether the view shows it
plainsolid compare FILE [OTHER | --rev HEAD~1] [-o overlay.png]
plainsolid export FILE -o out.step|out.stl   a drawing: out.svg|out.dxf|out.pdf
plainsolid docs                          this guide
plainsolid mcp [DIR]                     the same as MCP tools on stdio; DIR: cad/ in the checkout by default
plainsolid open FILE... | status | stop | install-launcher   the running app, for people (see the README)
```

Every command prints JSON. Exit codes: 0 ok, 1 an error (`{"error": ...}` on
stderr), 2 the file exists, 3 the hash was stale.

**MCP** (`claude mcp add plainsolid -- uv run plainsolid mcp /path/to/project`,
or the equivalent in another client). Tools take a `path` relative to the
project directory: `list_files`, `new_document`, `get_tree` (compact: results
once per feature; `compact=false` for everything), `get_feature` (one feature
in full), `get_source`, `edit`, `set_source`, `undo`, `redo`, `query`,
`measure`, `render` (the picture and a caption), `export`, `compare`. The resource `plainsolid://guide` is this
text. A GUI server watching the same directory shows every edit at once, and
the tools re-read a file the GUI changed.

## The loop

1. `tree` (or `new`) to see what is there: features in order, each with its
   arguments, `result.ok`, `result.error` (message and line), `result.faces_created`,
   a sketch's `result.sketch` (degrees of freedom `dof`, `free_variables` naming
   per entity what is still loose, such as `slot1: [angle]` or `line3: [end.x, end.y]`,
   `redundant` and `conflicting` constraints).
2. `edit` one operation, or a `batch`. The answer carries the unified diff,
   the new `hash`, and (over MCP) every `error` and `warning` of the evaluation.
   A failed feature is skipped and marked; the features after it continue on the
   previous body, so fix it before building on it.
3. `render` with `--highlight` to see that a selector picks what you mean before
   writing it into a fillet, a sketch plane or a mate; the caption lists each
   highlighted entity with its kind and size and says when the view does not
   show it, so an empty-looking picture is never mistaken for an empty selector.
   `measure` to check a distance. `query summary` for volume, area, bounding
   box, counts, centre of mass and mass.

Views: `front` looks along +Y from the −Y side with Z up, `back` from +Y,
`right` from +X, `left` from −X, `top` from above with Y up, `bottom` from
below, `iso` from the +X −Y +Z corner. The same names mean the same in the
browser and in drawings.
4. Repeat. `undo` reverts the last edit of the session.

Pass the `hash` from the previous answer with an edit when something else may
be editing the file (a person in the GUI); a stale hash is refused with the
current one.

## The model file

```python
from plainsolid import *

meta(name="bracket", material="al6061", revision="A", author="Paolo")

thickness = 4.0                      # a plain assignment is a parameter
hole_d = param(5.0, "clearance hole for M5")   # with a description for the GUI

profile = sketch("profile", on=XZ)
profile.line("bottom", (-30, 0), (30, 0))
profile.line("right", (30, 0), (30, 4))
profile.coincident("c1", "bottom.end", "right.start")
profile.horizontal("h1", "bottom")
profile.length("w", "bottom", 60)
profile.length("t", "right", thickness)
body = extrude("body", profile, 40)
inner = fillet("inner", body.edges.from_sketch("inner_bottom").from_sketch("inner_wall"), 3)
holes = sketch("holes", on=XY)
holes.project("back_edge", body.edges.where(parallel_to="+X").nearest((0, 0, 0)))
holes.circle("hole1", hole_d, at=(-10, -20))
holes.distance("y1", "hole1.center", "back_edge", 20)
cut("hole_cut", holes, through=True)
```

Rules. Every feature, entity and constraint has a unique string name, given
first; names are immutable. A feature statement is `name = call("name", ...)`
(the variable is how later statements refer to it), except `cut`, mates,
`dimension` and `note`, which are bare calls. Any numeric argument may be an
expression over parameters and named sketch dimensions (`profile.w`). Any
feature takes `suppressed=True`. Features created inside loops or functions
are read-only for the tools. Python runs with restricted builtins: no I/O,
no imports besides `plainsolid` and sibling modules of the project.

### Sketches

`sketch(name, on=XY|XZ|YZ | plane_feature | face_selector, offset=0, flip=False)`.
Coordinates are 2D in the sketch plane, in mm. On a body face they are
relative to the face's centre and oriented as a person looking at the face
sees it: on a wall or an inclined face y is up (global +Z projected onto the
face) and x is to the viewer's right; on a horizontal face x is global X. The
tree reports every sketch's frame as `result.plane` (`origin`, `x_dir`,
`y_dir`, `z_dir`).

| Entity | Statement | References it defines |
|---|---|---|
| line | `s.line("l1", (x1, y1), (x2, y2))` | `l1`, `l1.start`, `l1.end`, `l1.mid` |
| circle | `s.circle("c1", diameter, at=(x, y))` | `c1`, `c1.center` |
| arc | `s.arc("a1", center, start, end)` counter-clockwise | `a1`, `a1.center`, `a1.start`, `a1.end` |
| point | `s.point("p1", (x, y))` construction | `p1` |
| rect | `s.rect("r1", width, height, at=(x, y))` | `r1.center`, `r1.top`, `r1.bottom`, `r1.left`, `r1.right`, corners `r1.tl`, `r1.tr`, `r1.bl`, `r1.br`, `r1.width`, `r1.height` |
| slot | `s.slot("s1", length, width, at=(x, y), angle=0)` | `s1.center`, `s1.start`, `s1.end`, `s1.axis`, `s1.start_arc`, `s1.end_arc`, `s1.length`, `s1.width` |
| polygon | `s.polygon("p", [(x, y), ...])` | `p.p0`, `p.p1`, ... points, `p.e0`, `p.e1`, ... edges |
| project | `s.project("e1", body.edges.nearest((x, y, z)))` a body edge, vertex or face outline, fixed, follows the body; construction unless `construction=False` | `e1`, and `e1.start`, `e1.end`, `e1.center` as the geometry allows |
| offset | `s.offset("o1", ["l1", "a1"], 2, side="outside"|"inside"|"left"|"right", corners="sharp"|"round")` | `o1.e0`, `o1.e1`, ... |

Every entity accepts `construction=True`. Rect, slot and polygon are rigid
macros: their sides and points are references, not separate entities.

Constraints, each `s.kind("name", refs..., value)`:

| Kind | Arguments |
|---|---|
| coincident | two points |
| horizontal, vertical | a line, or two points |
| parallel, perpendicular | two lines |
| equal | two lines (lengths) or two circles/arcs (radii) |
| tangent | a line or circle and a circle or arc (`inside=True` for an inner tangent) |
| concentric | two circles or arcs |
| coradial | a circle or arc and a projected circular edge |
| colinear | two lines |
| symmetric | two points and the mirror line: `symmetric("s", "a", "b", "axis")` |
| midpoint | a point and a line |
| on | a point and a curve |
| fix | a point or an entity: pinned where it is |
| distance | two points, a point and a line, or two parallel lines, then the value; `along="x"` or `"y"` for a sheet-axis component. The side is taken from the seed: a point stays on the side of the line it was drawn on, and with `along` the second point stays on the side of the first it started on, so draw roughly where the result should be |
| length | a line and the value; or a size of a macro: `slot1.length` (tip to tip), `slot1.width`, `rect1.width`, `rect1.height` |
| diameter, radius | a circle or arc and the value |
| angle | two lines and the value in degrees, 0 to 180, between the lines' directions; `reverse=True` measures against the second line's opposite direction (the supplementary sector), so either angle of a V can be dimensioned without moving it |

Every dimension takes `at=(x, y)`, where the GUI shows its label in sketch
coordinates; it does not affect the geometry and a dimension without one gets a
computed place. Dimensions (`distance`, `length`, `diameter`, `radius`, `angle`) are named and
readable as `sketch.name` in later expressions. The tree reports each sketch's
degrees of freedom; a fully constrained sketch has `dof: 0`. Under-constrained
sketches still build geometry from the solved coordinates.

### Part features

| Feature | Statement |
|---|---|
| extrude | `extrude("e", sketch, depth, symmetric=False, flip=False, draft=0, op="add"|"cut", upto=face_selector)` |
| cut | `cut("c", sketch, depth, through=True, symmetric=False, flip=False, upto=face_selector)`; a cut aims into the material when only one side has any |
| revolve | `revolve("r", sketch, axis="centerline"|X|Y|Z, angle=360, op="add")`; the axis lies in the sketch plane, the profile on one side |
| fillet | `fillet("f", edge_selector or [selectors], radius)` |
| chamfer | `chamfer("c", edge_selector or [selectors], distance, distance2=None)` |
| shell | `shell("s", face_selector or None, thickness, outward=False)` |
| linear_pattern | `linear_pattern("p", feature, count, spacing=, direction=X, count2=, spacing2=, direction2=Y)` repeats an extrude, cut or revolve |
| circular_pattern | `circular_pattern("p", feature, count, axis=Z or an edge or cylindrical face selector, angle=360)` |
| mirror | `mirror("m", feature or None for the whole body, about=YZ or a plane feature or a planar face)` |
| plane | `plane("p", XY, offset=10)`, `plane("p", body.faces.top, offset=5)`, `plane("p", XY, angle=30, about=edge_selector)`, `plane("p", between=(a, b))`, `plane("p", through=(v1, v2, v3))` |
| import_step | `import_step("v", "vendor/file.step", tolerance=None)` an opaque body; `"vendor/file.step#node.sub"` one node of the file in its own coordinates (a sub-assembly node only in an assembly document, as a review of it) |

A part has one body: a cut that splits it is an error. Fillet and chamfer
consume the edges they round, so a selector for those edges resolves only up
to the feature before (`render --upto body --highlight ...`).

### Selectors

A selector names faces, edges or vertices by what made them, never by index:

```
body.faces.top                 the end face of an extrusion (.bottom the other)
body.faces.from_sketch("right")     the side face a sketch entity swept
body.edges.top.from_sketch("bottom")     one edge: two labels chained
rev.faces.start / .end         the caps of a partial revolve; shell adds .inner
body.faces.where(normal="+Z")  by normal (+X..-Z or a tuple), kind="plane"|"cylinder"|..., parallel_to= (edges)
body.edges.nearest((x, y, z))  the one closest to a point
body.faces.largest() / .smallest() / .all()
```

Labels come from the identity map: an extrusion's end faces carry `top` and
`bottom`, its side faces the sketch entity they came from (`rect1.left`,
`hole1`, `slot1.start_arc`, `poly.e2`, a projection's name); fillet and
chamfer faces take the labels of the edge they replaced; pattern and mirror
copies repeat them. The feature named first only matters for labels: `inner.faces`
alone is every face of the body, `inner.faces.from_sketch("inner_bottom")` the
fillet face. Chains that could match several entities must end in `nearest`,
`largest`, `smallest` or `all()`.

In `render --highlight` and `measure`, a selector is written exactly as in the
file; a bare feature name highlights everything that feature owns on the final
body (`--highlight hole_cut`).

### Assemblies

```python
meta(kind="assembly", name="node")
box = instance("box", "enclosure.py")
lid = instance("lid", "lid.py", color="#d8d8d0", at=(0, 0, 50), rotate=(0, 0, 0))
gland = instance("gland", "vendor/gland_m12.step", material="nylon")
fixed("anchor", box)
coincident("lid_down", lid.faces.of("plate").bottom.largest(), box.faces.of("body").top)
coincident("lid_x", lid.planes.YZ, box.planes.YZ)
concentric("gland_axis", gland.faces.where(kind="cylinder").largest(), box.faces.of("gland_cut").from_sketch("gland1"))
distance("gap", lid.planes.XY, box.planes.XY, 20)
angle("tilt", panel.faces.where(normal="-Z").largest(), lid.faces.of("plate").top, 15)
```

Mates: `fixed`, `coincident`, `concentric`, `distance` (value), `parallel`,
`angle` (degrees); `flip=True` reverses the orientation. References are in the
part's own coordinates; `inst.faces.of("feature")` narrows to what one feature
of the part made, `inst.planes.XY` and `inst.axes.Z` name its standard planes
and axes. Vendor STEP instances have no labels: geometric selectors only. The
solver writes the solved `at=` and `rotate=` back into every instance
statement after each edit, so the file holds what is shown. The tree's
`evaluation.assembly` reports `dof`, `free` instances, `redundant` and
`conflicting` mates. Queries: `bom`, `mass`, `interference`.

### Drawings

```python
meta(kind="drawing", name="bracket_dwg", of="bracket.py", sheet="A4", scale=1, revision="A")
front = view("front", direction=FRONT, at=(65, 140))
top = view("top", direction=TOP, at=(65, 62), hidden=True)
sec = view("sec", section=YZ, offset=-10, at=(150, 66))
dimension("width", front.edges.of("body").bottom.from_sketch("outer_wall"), front.edges.of("body").bottom.from_sketch("right"), at=(0, -38))
dimension("hole", top.faces.of("hole_cut").from_sketch("hole1"), at=(-24, 16), text="2× Ø5")
note("finish", "Break all edges 0.5 mm", at=(12, 28))
```

`at` on a view is its centre on the sheet in mm from the bottom-left corner;
on a dimension or note it is relative to the view's centre. One reference
gives a diameter (or `kind="radius"`), two a distance (`along="x"|"y"`) or,
with `kind="angle"`, an angle. Export to `svg`, `dxf` or `pdf`.

## Edit operations

Every operation is a JSON object with `op`, plus an optional `hash`. A value
that should be written as an expression rather than a number is
`{"expr": "wall * 2"}`; selectors and feature handles in `args` are written the
same way (`{"expr": "body.faces.top"}`).

| op | Fields |
|---|---|
| `set_parameter` | `name`, `value` (a whole number written over `2.0` stays a float) |
| `add_parameter` | `name`, `value`, optional `description`; inserted after the last parameter so every feature can use it |
| `delete_parameter` | `name`; refused while anything uses it |
| `set_argument` | `feature`, `kwarg`, `value` (a keyword of the feature's statement; `null` drops it) |
| `set_meta` | `key`, `value` (one keyword of `meta()`; the kind cannot change) |
| `add_feature` | `kind`, `name`, `args`, optional `after` (a feature name; default: appended). Or `statement` with the literal source line |
| `delete_feature` | `feature`, `cascade` (default true: everything that depends on it goes too) |
| `add_sketch_entity` | `sketch`, `kind`, `name`, `args` (the positional arguments by name plus `at`, `angle`, `construction`). Or `statement` |
| `delete_sketch_entity` | `sketch`, `entity` |
| `set_entity_argument` | `sketch`, `entity`, `kwarg`, `value` |
| `add_constraint` | `sketch`, `kind`, `name`, `refs` (list of reference strings), `value` for dimensions, `options` (`{"along": "x"}`, `{"inside": true}`, `{"reverse": true}`, `{"at": [x, y]}`). Or `statement` |
| `delete_constraint` | `sketch`, `constraint` |
| `set_constraint_value` | `sketch`, `constraint`, `value` |
| `set_constraint_argument` | `sketch`, `constraint`, `kwarg` (`at`, `along`, `reverse`, `inside`), `value` (null drops the keyword) |
| `batch` | `ops`: a list of operations applied in order as one edit |
| `replace_source` | `source`: the whole file |
| `write_back`, `write_poses` | what the GUI uses to store solved coordinates and poses; every sketch and assembly edit already does this |
| `explode_import`, `make_editable` | turn a review import of a STEP file into instances, and a vendor body into a part file |

`add_feature` args by kind:

| kind | args |
|---|---|
| sketch | `on` (`"XY"`, a plane feature `{"expr": "p1"}`, a face selector), `offset`, `flip` |
| extrude, cut | `sketch` (the sketch's name), `depth` or `through: true` or `upto`, `symmetric`, `flip`, `draft`, `op` |
| revolve | `sketch`, `axis` (`"X"`, `"Y"`, `"Z"`, or the name of a construction line of the sketch), `angle`, `op` |
| fillet | `edges` (a selector, or a list), `radius` |
| chamfer | `edges`, `distance`, `distance2` |
| shell | `faces` (or null), `thickness`, `outward` |
| linear_pattern | `feature`, `count`, `spacing`, `direction`, `count2`, `spacing2`, `direction2` |
| circular_pattern | `feature`, `count`, `axis`, `angle` |
| mirror | `feature` (optional), `about` |
| plane | `base`, `offset`, `angle`, `about`, `between`, `through`, `flip` |
| import_step | `path`, `tolerance` |
| instance | `path`, `at`, `rotate`, `color`, `material`, `density`, `tolerance` |
| fixed | `instance` |
| coincident, concentric, parallel | `a`, `b`, `flip` |
| distance, angle | `a`, `b`, `value`, `flip` |
| view | `direction` or `section` (+ `offset`, `flip`), `at`, `scale`, `hidden` |
| dimension | `a`, `b`, `at`, `kind`, `along`, `text` |
| note | `text`, `at`, `size`, `view` |

A sketch's entities and constraints go in as `add_sketch_entity` and
`add_constraint` after the `add_feature` of the sketch; a `batch` does the
whole sketch in one edit and one solve. Every sketch edit re-solves the sketch
and writes the solved coordinates back into the entity statements, so the
file's numbers are always the solved ones.

## A worked session: the bracket

The L-bracket of the reference zoo, built through the CLI alone (the test
`tests/test_agent.py` runs exactly this and checks the golden geometry).

```
plainsolid new bracket.py --name bracket
plainsolid edit bracket.py '{"op": "add_parameter", "name": "thickness", "value": 4.0}'
plainsolid edit bracket.py '{"op": "add_parameter", "name": "hole_d", "value": 5.0, "description": "clearance hole for M5"}'
```

Then the profile sketch, in one batch:

```json
{"op": "batch", "ops": [
  {"op": "add_feature", "kind": "sketch", "name": "profile", "args": {"on": "XZ"}},
  {"op": "add_sketch_entity", "sketch": "profile", "kind": "line", "name": "bottom", "args": {"start": [-30, 0], "end": [30, 0]}},
  {"op": "add_sketch_entity", "sketch": "profile", "kind": "line", "name": "right", "args": {"start": [30, 0], "end": [30, 4]}},
  {"op": "add_sketch_entity", "sketch": "profile", "kind": "line", "name": "inner_bottom", "args": {"start": [30, 4], "end": [-26, 4]}},
  {"op": "add_sketch_entity", "sketch": "profile", "kind": "line", "name": "inner_wall", "args": {"start": [-26, 4], "end": [-26, 50]}},
  {"op": "add_sketch_entity", "sketch": "profile", "kind": "line", "name": "top", "args": {"start": [-26, 50], "end": [-30, 50]}},
  {"op": "add_sketch_entity", "sketch": "profile", "kind": "line", "name": "outer_wall", "args": {"start": [-30, 50], "end": [-30, 0]}},
  {"op": "add_constraint", "sketch": "profile", "kind": "coincident", "name": "c1", "refs": ["bottom.end", "right.start"]},
  {"op": "add_constraint", "sketch": "profile", "kind": "horizontal", "name": "h1", "refs": ["bottom"]},
  {"op": "add_constraint", "sketch": "profile", "kind": "fix", "name": "origin", "refs": ["bottom.start"]},
  {"op": "add_constraint", "sketch": "profile", "kind": "length", "name": "w", "refs": ["bottom"], "value": {"expr": "width"}},
  {"op": "add_constraint", "sketch": "profile", "kind": "equal", "name": "wall_t", "refs": ["top", "right"]}
]}
```

Then the body and its rounds:

```json
{"op": "add_feature", "kind": "extrude", "name": "body", "args": {"sketch": "profile", "depth": {"expr": "base_depth"}}}
{"op": "add_feature", "kind": "fillet", "name": "inner", "args": {"edges": {"expr": "body.edges.from_sketch(\"inner_bottom\").from_sketch(\"inner_wall\")"}, "radius": 3}}
{"op": "add_feature", "kind": "chamfer", "name": "outer", "args": {"edges": {"expr": "body.edges.from_sketch(\"bottom\").from_sketch(\"outer_wall\")"}, "distance": 2}}
```

Holes placed from projected edges, then cut through:

```json
{"op": "batch", "ops": [
  {"op": "add_feature", "kind": "sketch", "name": "holes", "args": {"on": "XY"}},
  {"op": "add_sketch_entity", "sketch": "holes", "kind": "project", "name": "back_edge", "args": {"selector": {"expr": "body.edges.where(parallel_to=\"+X\").nearest((0, 0, 0))"}}},
  {"op": "add_sketch_entity", "sketch": "holes", "kind": "circle", "name": "hole1", "args": {"diameter": {"expr": "hole_d"}, "at": [-10, -20]}},
  {"op": "add_constraint", "sketch": "holes", "kind": "diameter", "name": "d1", "refs": ["hole1"], "value": {"expr": "hole_d"}},
  {"op": "add_constraint", "sketch": "holes", "kind": "distance", "name": "y1", "refs": ["hole1.center", "back_edge"], "value": {"expr": "base_depth / 2"}},
  {"op": "add_feature", "kind": "cut", "name": "hole_cut", "args": {"sketch": "holes", "through": true}}
]}
```

Two slots through the wall, sized by their overall length and width and placed
from projected edges of the body, no `fix` anywhere:

```json
{"op": "batch", "ops": [
  {"op": "add_feature", "kind": "sketch", "name": "slots", "args": {"on": "YZ"}},
  {"op": "add_sketch_entity", "sketch": "slots", "kind": "project", "name": "wall_top", "args": {"selector": {"expr": "body.edges.from_sketch(\"top\").from_sketch(\"outer_wall\")"}}},
  {"op": "add_sketch_entity", "sketch": "slots", "kind": "project", "name": "wall_end", "args": {"selector": {"expr": "body.edges.top.from_sketch(\"outer_wall\")"}}},
  {"op": "add_sketch_entity", "sketch": "slots", "kind": "slot", "name": "slot1", "args": {"length": 16, "width": 5, "at": [-32, 30], "angle": 90}},
  {"op": "add_constraint", "sketch": "slots", "kind": "vertical", "name": "upright1", "refs": ["slot1.axis"]},
  {"op": "add_constraint", "sketch": "slots", "kind": "length", "name": "slot_len", "refs": ["slot1.length"], "value": 16},
  {"op": "add_constraint", "sketch": "slots", "kind": "length", "name": "slot_w", "refs": ["slot1.width"], "value": 5},
  {"op": "add_constraint", "sketch": "slots", "kind": "distance", "name": "down", "refs": ["slot1.center", "wall_top"], "value": 20},
  {"op": "add_constraint", "sketch": "slots", "kind": "distance", "name": "in", "refs": ["slot1.center", "wall_end"], "value": 8},
  {"op": "add_feature", "kind": "cut", "name": "slot_cut", "args": {"sketch": "slots", "through": true, "flip": true}}
]}
```

Check as you go:

```
plainsolid render bracket.py -o body.png --highlight 'body.faces.top' --highlight hole_cut
plainsolid measure bracket.py 'body.faces.top' 'body.faces.bottom'
plainsolid query bracket.py summary
plainsolid compare bracket.py --rev HEAD -o changes.png
plainsolid export bracket.py -o bracket.step
```

## Reading the tree

`tree` returns `{kind, meta, params: [{name, value, expression, description}],
features: [...], errors: [...], evaluation: {...}, names: {params, dimensions},
hash}`. Each feature carries `kind`, `args` (evaluated values), `line`,
`read_only`, `entities` and `constraints` for a sketch, and `result`: `{ok,
skipped, error: {message, line}, warnings, faces_created, sketch: {dof,
free_entities, free_variables, redundant, conflicting}, plane}`. The
evaluation of an assembly adds `assembly: {dof, free, redundant, conflicting,
poses}`; a drawing's adds a summary of its views and dimensions. That is the
compact form the MCP `get_tree` returns; `get_feature` (or the CLI's `tree`)
adds the source text of every argument, a sketch's solved coordinates and
`dependents`, what a delete would cascade to.

## Pitfalls

- Selectors resolve against the body as it exists at their place in the tree.
  A fillet's edge is gone after the fillet. Look with `--upto`.
- `through=True` is for cuts. An extrude needs a depth, `upto=` or `through`
  with `op="cut"`.
- A `cut` aims into the material when only one side has any; `flip=True`
  reverses a two-sided case.
- A revolve's axis must lie in the sketch plane with the whole profile on one
  side; the engine refuses a profile that crosses it.
- A sketch on a face uses coordinates relative to the face's centre.
- Names cannot be reused, even after a delete in the same batch fails; check
  the tree for what exists.
- Setting a keyword back to its default drops it from the statement, so files
  stay as a person would write them.
