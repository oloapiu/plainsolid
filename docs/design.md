# plainsolid: design

A minimal parametric CAD tool in the spirit of SolidWorks, built as a
bidirectional GUI over a code model.

- The source of truth is a human-readable Python file. The GUI is a view and
  editor of that file: a click in the GUI edits the code, an edit to the code
  updates the 3D view. Humans and agents edit the same file, and it diffs
  cleanly in git. There is no binary or hidden model state.
- Geometry is [build123d](https://github.com/gumyr/build123d) on OCCT. We
  write no kernel code and do not wrap OCCT where build123d already covers it.
- The engine runs as a local server exposing an API. The browser UI is a
  strict client of that API with no privileged access.

It exists for simple mechanical engineering (sensor enclosures, mounts,
shields) that does not justify a SolidWorks seat. It is human first; agent use
comes from the same API. The workflow it serves: mock a design to a fair level
of detail, export STEP to a contract manufacturer, import the manufacturer's
DFM proposals as STEP assemblies, review them with sections and measurements,
snapshot views into feedback, iterate, and occasionally produce 2D drawings.
The largest assembly it targets is about ten parts, several of them vendor
STEP files.

This document records the decisions and the architecture. The testing
approach is in [testing.md](testing.md), the browser client in
[web/README.md](../web/README.md), and the language as an agent sees it in
`src/plainsolid/agent.md` (`plainsolid docs`).

## Decisions

### The model file

| Decision | Choice |
|---|---|
| Format | A Python file using a small declarative API from `plainsolid`. The file is executed to produce a feature tree. |
| GUI edits | Surgical patches through libcst on the exact statement touched. The file is never regenerated from the tree. Comments and formatting survive. |
| What the GUI can edit | Top-level feature statements and top-level numeric assignments. Features created inside loops or functions appear in the tree as read-only. |
| Python allowed | Any Python executes, with restricted builtins: no file I/O, no network, no randomness, no time. Sibling imports inside the project directory are allowed for shared parameters. |
| Parameters | Plain top-level numeric assignments are parameters. An optional `param()` wrapper carries a description for the GUI. |
| Expressions | Every numeric field in the GUI accepts an expression over parameters and named sketch dimensions, with autocomplete. Unnamed dimensions stay literal. |
| Bodies | One file, one part, one solid body. A cut that splits the body is an error. |
| Names | Every feature and sketch entity has a unique string name, auto-numbered (`extrude1`, `line3`). Names are immutable; renaming with cascade is not supported. |
| Metadata | A `meta()` line with name, material, revision, author and free-form extras. It feeds title blocks and the bill of materials. |
| Units | Millimetres and degrees, no unit system. |
| Determinism | Same file, same geometry, always. Enforced by the restricted namespace. A portability guarantee, not a security boundary: model files are the user's own code, like a notebook. |

Why Python over a data format: agents and humans are fluent in it, expressions
come for free, and libcst makes lossless round trips possible. Why not raw
build123d: the GUI could only read it.

### References and failure

| Decision | Choice |
|---|---|
| Reference families | Semantic (the side face from sketch entity `rect1.top`, the top face of `extrude1`) when derivable; geometric predicates (normal, nearest point, largest, kind) otherwise. |
| Raw indices | Rejected by the parser with a message naming the selector to use instead. |
| Failed feature | Skipped and marked with the error and its source line. Downstream continues on the previous body. |
| Suppression | Any feature accepts `suppressed=True`. |
| Rollback | Evaluate up to feature N is an API option and a bar in the GUI tree. |
| Dangling projection | A projected sketch entity whose selector fails marks the sketch with an error; the rest of the sketch still solves. |

### Sketches

| Decision | Choice |
|---|---|
| Entities | point, line, arc, circle. Macros: rectangle, slot, polygon, expanding into entities plus constraints. Derived: `project` (body geometry that follows the body), `offset` (curves at a distance from other curves) and `import_dxf` (the curves of a DXF file). Any entity can be construction geometry, and a rect or polygon can draw some of its sides as construction (`construction=["top"]`) so the rest closes with other curves. No splines. |
| Constraints | coincident, midpoint, on, distance, parallel, perpendicular, equal, horizontal, vertical, tangent, concentric, symmetric, fix, and dimensions (length, diameter, radius, angle). Coincident, coradial and colinear against projected body geometry. `length` also takes a macro's size (`slot1.length`, `slot1.width`, `rect1.width`, `rect1.height`). |
| Built-in references | Every sketch has `origin`, `x_axis` and `y_axis` without declaring them: fixed, drawn as a marker and two faint dashed lines, pickable in every mode (the sketch's own geometry wins a tie, the origin wins over the axes and over a line through it), taking the relations a fixed point or line takes. Entities cannot take the names; constraints can, they are never referenced. |
| Projection | A sketch can project body edges, vertices and face outlines as linked entities. They are fixed for the solver, update when upstream changes, and are usable as profile geometry. They recompute after every solve and are never written back. |
| DXF import | `s.import_dxf("outline", "plate.dxf", layer="CUT", at=(0, 0), angle=0)` reads a DXF file's lines, arcs and circles (polylines with bulges and blocks exploded, splines and ellipses as lines within 0.01 mm) in millimetres from `$INSUNITS`, a file without units read as millimetres with a warning. Ends within 5 µm are joined so the outline closes exactly; ends that stay open are reported, with the gaps under 1 mm and where they are. Text, dimensions and fills are left out silently. The curves are one rigid piece: three solver variables, where the file's origin lands (`at`) and the angle, written back like any entity's; a drag translates it; `fix` pins it. Its curves are references like a projection's (`outline.e3`, `outline.e3.start`) plus `outline.origin`, and label the faces they sweep. The file is a dependency: a new revision rebuilds the part and shows in compare. "Convert to lines" (`convert_dxf`) writes the curves as plain entities where they stand, joined by coincidents, moving the relations on its curves to them and dropping those on the import as a whole; it refuses past 300 curves. |
| Auto-inference | Horizontal, vertical, coincident, midpoint, on-curve and tangent while drawing. Inferred constraints are written into the file as real statements. |
| Constraint UX | Draw loosely, select entities, pick a constraint from the right-click menu or the panel's selected section, both listing only what is valid for the selection. The dimension tool works like Smart Dimension: click one or two entities, the dimension follows the cursor, a click on empty space places it and opens the value field, and the tool stays on until stopped. For two points the label's side decides horizontal, vertical or aligned; for two lines the sector it sits in decides which angle. Dimensions draw with extension lines, a dimension line through the label and arrowheads. |
| Dimension labels | Where a label sits is an `at=` keyword on the dimension statement, written when the label is placed or dragged, so the layout travels with the file. A dimension without one gets a computed place. |
| Trim | A tool (t): the piece of a line, arc or circle under the cursor goes, up to the nearest crossings with any other curve, construction and converted geometry included. A cut end is related to the curve it was cut at; a line cut in the middle becomes two colinear lines, an arc two coradial arcs; a circle cut between two crossings becomes an arc of the same name; a curve with no crossing goes entirely. Relations on the removed part go, a length on a shortened line is dropped. Macro sides cannot be trimmed: their outline would have to be drawn with lines. Composed on the server like the fillet. |
| Fillets and chamfers | Right-click a corner, or a selection of corners or of two lines meeting at one, choose fillet or chamfer, type the size. Two lines get an arc (or a bevel line) with coincidences and tangencies and a virtual sharp: a construction point held on both lines where the corner was, so what referenced the corner references the sharp and a length on either line becomes a distance to it; nothing moves. A corner of a rect or polygon becomes an entry of the macro's `corners=` (or `chamfers=`) argument: the corner handles stay the sharp corners, the sides run sharp to sharp for relations, the profile is trimmed, and each cut is a reference of its own (`plate.tl_arc`) with a centre and tangent points, so radius, equal, concentric and tangent work on it. Every fillet is dimensioned: one radius on the first, equal relations on the rest made together; the number form `corners=8` is kept while all radii agree. A radius that does not fit fails the sketch naming the corner. Removal takes either apart again. The tangency of an arc that meets a line at its end is held as the radius being perpendicular to the line, since the distance form has a vanishing gradient there. |
| Write-back | After every committed GUI operation (drag end, dimension change) the solved coordinates are written back at four decimals, skipping values unchanged within tolerance. Expressions are left alone. |
| Feedback | Under-constrained entities blue, fully constrained white, conflicting red; the remaining degrees of freedom are shown and named. |
| Sketch frame on a face | As a person looking at the face sees it: on a wall or an inclined face y is global +Z projected onto the face and x is the viewer's right; on a horizontal face x is projected global X. Coordinates are relative to the face centre. The tree reports every sketch's frame. |
| Solver | Hand-rolled on `scipy.optimize.least_squares`: a residual per constraint, analytic Jacobians, rank for degrees of freedom and over-constraint detection, drag as a constrained tangent step plus projection. python-solvespace has no wheels past Python 3.11. |

### Part features

| Feature | Options |
|---|---|
| extrude | blind, symmetric, through all, up to a face, two directions with separate depths, draft angle; operation add, cut or intersect; `cut()` is an alias |
| revolve | axis from a sketch construction line or a global axis, any angle; the profile plane must contain the axis |
| fillet | constant radius on selected edges |
| chamfer | equal distance, distance-distance |
| shell | faces to remove, thickness, inward or outward |
| linear_pattern, circular_pattern | one or two directions, or a count and total angle; repeats a feature; patterns and mirrors are themselves repeatable |
| mirror | across a plane or planar face; with no feature it mirrors the whole body |
| plane | offset from a plane or face, angled about an edge, midplane between parallel faces, through three points |
| import_step | an opaque body from a STEP file, or one node of it (`"file.step#node.path"`) |

Cuts aim into the material when only one side has any. Out of scope: sheet
metal, loft, sweep, hole wizard, threads, text, surfaces, variable fillets,
multi-body parts.

### Assemblies

| Decision | Choice |
|---|---|
| Format | The same Python subset. Instances of part files or vendor STEP files directly; nesting is flat. |
| Poses | Each instance statement carries its pose (`at=`, `rotate=` in degrees about X, Y, Z in build123d's Location sense). After every edit the solved poses are written back at four decimals, like sketch coordinates, so the file holds what is shown. |
| Mates | fixed, coincident, concentric, distance, parallel, angle. References are in the part's own coordinates: `lid.faces.of("plate").bottom`, `gland.faces.where(kind="cylinder").largest()`, `box.planes.XZ`, `gland.axes.Z`. Two body faces mate face to face; planes, axes and points align; `flip=True` reverses. The GUI picks the orientation that turns the parts less when a mate is added and writes it as an explicit `flip=`. |
| Vendor geometry | Geometric predicates only, since it carries no semantic names. Vendor files are never modified in place; a part file that imports a STEP node and adds cuts is the path for that. |
| STEP nodes | `"vendor/node.step#node.glands.gland_1"` names one body inside a STEP file, in that body's own coordinates, for `instance` and `import_step` alike. A sub-assembly node imported into an assembly document is a review of that sub-assembly in its own coordinates. A review import turns into such instances on the first "make editable"; a body gets a part file wrapping its node, and every instance of the same product follows that part. No STEP file is ever split or rewritten. |
| Solver | Six degrees of freedom per instance, mate residuals, the same least-squares core as sketches. The first fixed instance anchors the assembly. Under-constrained instances stay nearest their current pose. Wholly redundant and conflicting mates are warnings on the mate, never errors. |
| Colour | On the part, with a per-instance override. Colour is the only styling. |
| Mass | Density from a material name in a small built-in table or an explicit number. |
| Queries | Interference between instances, mass properties, bill of materials. |
| In-context editing | Not supported. Parts are edited in their own document; the assembly follows through the file watcher and the dependency revision. |

### Review tools

| Decision | Choice |
|---|---|
| STEP import | Preserves hierarchy, names and colours as a clickable instance tree. Opening a foreign STEP creates a wrapper model file next to it: a part when the file holds one solid, a review assembly otherwise. plainsolid walks the XCAF document itself, naming each node from the component label first and the product label second, because build123d's importer uses only the product name and so collapses instances of shared geometry. A sub-assembly of the file opens in a tab of its own from its row's menu, and after "make editable" from the grey group row above its bodies or from a body's own menu: it gets a wrapper named after the node (`node.glands.py` next to `node.step`, importing `node.step#node.glands`) and shows in its own coordinates, with a sidecar of its own. |
| Sections | One active plane, from any standard plane, reference plane or planar face plus offset, with a slider. Applies to everything visible; cut faces are capped. |
| Measurements | Point to point, minimum distance between any two entities, edge length, circle diameter and centre, angle between faces or edges, x/y/z components. Pinnable so they stay on screen. |
| Snapshot | The current viewport including pins and section, to the clipboard and to a PNG file. |
| Compare | An overlay of this document against another file or its own last commit: common grey, added green, removed red, with a volumetric change report per region. |

### Drawings

Views and manual dimensions, not full drafting.

| Decision | Choice |
|---|---|
| Document | A third document type: `meta(kind="drawing", of="part.py", sheet="A4", scale=1)` plus `view`, `dimension` and `note` statements. |
| DXF views | `view("old", dxf="old.dxf", at=(148, 117), layer=None)` shows a DXF file as it is, its extents centred on `at`: curves (dashed where the linetype is), text, dimensions and leaders exploded into lines, arrowheads and text, hatch patterns as lines, solid fills filled. A drawing of DXF views alone needs no `of`; its title block names the files. Notes label DXF views; dimensions refuse them (they measure the model). |
| Sheets | A4, A3, letter, landscape; coordinates in mm from the bottom-left corner. `at` on a view is where its centre lands; `at` on a dimension or note is relative to its view's centre, so dragging a view carries them. |
| Views | front, top, right, left, back, bottom, isometric, with hidden lines; a section from a plane and offset, keeping the side behind the plane's normal, hatched, lettered in file order. The template lays out four third-angle views. |
| Dimensions | One `dimension` kind: two references give a distance or, with `kind="angle"`, an angle; one gives a diameter or, with `kind="radius"`, a radius. References go through the view handle (`front.edges.of("body").from_sketch("x")`) and resolve on the model with the ordinary selectors. |
| Annotations | Text notes; the title block takes the model's `meta()` unless the drawing's own sets a field. |
| Export | SVG and DXF through build123d's exporters; PDF drawn directly on reportlab, which also draws the sheet, title block, dimensions and notes. Dimensions render blue on screen and black in the exports. |
| Out of scope | Tolerances, detail views, bill of materials tables. |

### GUI

| Decision | Choice |
|---|---|
| Priority | Human first. Inspection and corrective edits must be easy; from-scratch modelling by hand is the goal. |
| Conventions | SolidWorks where they cost nothing. A trackball camera (the up vector follows, the standard views straighten it), middle or left drag orbits, shift plus middle drag pans, scroll zooms at the cursor, ctrl plus a number sets a standard view, f fits, escape cancels, ctrl-z and ctrl-shift-z undo and redo. Sketch mode: l c a r s p o draw, t trim, d dimension, e project, x construction. |
| Layout | Feature tree left, viewport centre, property panel right, code pane below. The side panels resize and collapse; the code pane resizes and hides. Three-way highlighting between tree, viewport and code. |
| Editing surface | The property panel and right-click menus are primary, the code pane secondary. Every dialog previews its result through a no-write server evaluation before anything is written. |
| Selection | Faces and edges pick everywhere, vertices only where a tool wants them. A click writes the most specific stable selector: semantic when the identity map has a label, otherwise a predicate plus a reducer. |

### Server, CLI and files

| Decision | Choice |
|---|---|
| Server | FastAPI, one per project directory, many documents, bound to localhost, no auth. |
| Edits | Every GUI action is an edit operation carrying the file hash it was computed against. A stale hash is rejected; the client reloads and retries. Operations are validated against typed contracts before anything runs; the browser's edit types are generated from them. |
| Writes | Immediate and atomic: the new text is written to a temporary file and moved into place after a last check of the disk; memory and the undo stack change only after the write succeeds. The file is the truth, git is the history. |
| Undo | A server-side stack of file snapshots per document. |
| Revisions | A document's geometry revision hashes its source and the stamps of every file it depends on, transitively. Meshes, comparisons, queries and previews are keyed on it, so a change to an instanced part or an imported STEP file invalidates everything derived from it. |
| Kernel | All kernel work runs on one worker thread per server, because documents share OCCT shapes through the part cache. |
| Code pane | Saves on a short debounce, in order, per document. A failed save keeps the draft and blocks closing or switching until it is saved or reloaded; a file changed underneath the draft becomes an explicit conflict with reload and overwrite choices. |
| Watching | The server watches the project directory and pushes new geometry and errors over a WebSocket. |
| CLI | Runs the engine in-process. JSON output on every command. The MCP server is the same engine as tools on stdio, re-reading a file the GUI changed before every call. |
| View state | Camera, named views, section, per-instance visibility and transparency live in a sidecar JSON next to each document. Git-tracked, ignored if missing or malformed, never referenced by the model file. |
| Mesh cache | Product tessellations are kept in memory and on disk (`.plainsolid-cache/`, gitignored), keyed by file, node and tolerance, never by pose. Derived state only. |
| Layout | The project is one directory: `cad/` in the checkout by default (gitignored), or the one given to `serve`. Documents are only ever opened and written inside it. Flat, with `vendor/` for bought STEP files and `proposals/` for the manufacturer's. |
| Files from outside | A STEP or DXF file from elsewhere is copied into the project, never opened in place: one dialog for folder and name, reached by dropping the file on the window, by `plainsolid open FILE`, or by the launcher `install-launcher` writes (a Finder Quick Action on macOS, a desktop entry on Linux), which calls the executable by its full path so nothing else needs setting up. |
| Opening a DXF | Always asks, in the same dialog, part or drawing (the last choice preselected); a DXF already in the project asks only that. A part writes `x.py` next to `x.dxf`: a sketch on XY importing it, fixed, and no feature after it, since what to make of the curves (an extrude, a cut, a revolve, one loop of several) is the person's choice and the sketch alone never fails; the first open, the one that writes it, enters that sketch, because a part without a body shows nothing; a drawing writes `x_dwg.py` (one DXF view on the smallest of A4 and A3 at the largest of 1:1 down to 1:100 that holds it). Each wrapper is written on the first open in its mode and reused after. The server refuses a DXF open or import without a mode. |
| Lifetime | `open` hands files to the open tabs through a workspace socket and raises a browser only when none is open; it starts a server when none answers, detached, with an idle exit thirty minutes after the last tab and request. `stop`, `status` and "quit server" in the file menu talk to the same server. |
| Export | STEP for parts and posed assemblies with names and colours, STL; drawings to SVG, DXF and PDF. |

### Tooling

| Area | Choice |
|---|---|
| Python | 3.13, managed with uv |
| Geometry | build123d 0.11.x, which depends on `cadquery-ocp-novtk` |
| Rendering | pyvista on vtk, added as our own dependencies because the OCP build is VTK-free |
| Editing | libcst |
| Solver | numpy, scipy |
| Server | FastAPI, uvicorn with its standard extras (the WebSocket endpoint needs them), watchfiles, pydantic for the edit contracts |
| CLI | typer; the MCP server on the `mcp` SDK |
| Drawings | ezdxf for DXF, reportlab for PDF |
| Client | Vite, TypeScript, React, three.js, CodeMirror 6; Playwright for browser tests |
| CI | GitHub Actions on Linux; macOS and Linux supported, Windows untested |
| License | Apache 2.0 |

## Architecture

### Layers

```
 model file (.py)  <----- libcst patches -----  edit layer
      |                                            ^
      | exec + libcst span mapping                 | edit ops (JSON)
      v                                            |
 feature tree  --->  evaluator (build123d)  --->  server (FastAPI + WS)
                          |                        ^          ^
                          v                        |          |
                  queries, tessellation,          CLI      browser client
                  render, export                (in-process) (three.js,
                                                              CodeMirror)
```

Each layer is testable without the one above it. All modules live flat in
`src/plainsolid/`.

- **Engine.** `dsl` (the API a model file calls), `model` (the tree
  dataclasses), `parse` (execute plus span mapping), `evaluate` (build123d
  evaluation, the identity map, the prefix cache), `selectors`, `sketchgeom`,
  `solver/`, `projection` and `offset` (derived sketch entities), `assembly`
  and `stepimport`, `drawing`, `query`, `measure`, `refs` (selectors written as
  text, ids in a mesh), `compare`, `section`, `mesh`, `render`, `io`,
  `materials`, `views` (the sidecar), `dependencies` (file stamps and
  revisions). Pure Python, no server.
- **Edit layer.** `edit` (operations to libcst patches), `operations` (the
  typed contracts), `literals` (Python source for values). Each operation
  takes source text and returns patched text plus a diff. This is the only way
  any client changes a file.
- **Workspace and server.** `workspace` (open documents, locks, atomic
  writes, undo, caches, previews), `server` (HTTP, WebSocket, the file watcher,
  the kernel worker), `cli`, `mcpserver`, and `agent.md`, the guide the MCP
  server serves as a resource.
- **Client.** `web/`, holding no model state beyond API responses.

### Parsing and span mapping

Two passes over the same text.

1. Execute the file in a restricted namespace with the `plainsolid` DSL
   imported. Each DSL call records itself into a `Document`: an ordered list
   of features, each with its arguments and the calling frame's line number.
2. Parse with libcst and match top-level statements to features by the name
   argument. A feature whose recorded line lies inside a loop, function or
   comprehension, or which cannot be matched to a top-level statement, is
   marked read-only.

The tree carries, for each feature: name, kind, arguments (evaluated values
plus the source text of each argument expression), the source span, the
read-only flag, and after evaluation the resulting shape, errors and the face
and edge identity map.

### Evaluation

Features evaluate in file order into a single build123d solid. Every feature
receives the current body and returns the new one. Selectors resolve against
the body as it exists at that point in the tree.

**Identity.** After each feature the evaluator records, per face and edge, the
feature that created it and a tuple of tags. An extrusion's end faces carry
`:top` and `:bottom` (labels start with a colon so a sketch entity called
`top` cannot collide) and its side faces the sketch entity they came from
(`rect1.left`, `hole1`, `slot1.start_arc`, a projection's name); a revolve's
faces carry their profile entity and a partial revolve's caps `:start` and
`:end`; fillet and chamfer faces take the tags of the edge they replaced;
shell walls add `:inner`; pattern and mirror copies repeat their source's
tags. A face a later feature splits or trims keeps its owner and tags, matched
by surface and by containment of its edge midpoints. An edge's tags are the
union of its faces' tags. The map goes to the client in the mesh header and
drives click-to-selector.

**Caching.** Every evaluation records, per feature, a signature chained over
all features before it (evaluated arguments, entities, constraints,
suppression, and for imports the stamps of the files they read) and a
snapshot of the state after it. The next evaluation of the same document
restores the longest unchanged prefix, so a sketch drag or a tail edit
re-evaluates only what changed.

**Assemblies.** An `instance` feature loads its file (a part evaluated in its
own coordinates with its identity map, or a STEP file as one rigid compound)
into a cache keyed by the file's stamps. Mates resolve their references at
their place in the tree to a plane, an axis, a circle or a point in the
instance's local frame; the solve runs once after the last feature, so a
rollback solves only the mates above it. The posed instances feed the same
mesh, section, measurement and query code as review imports. For the mesh,
each product is tessellated once in its own coordinates and every instance is
placed by transforming the arrays.

**Validation and errors.** Features check their inputs where the kernel would
fail silently (revolve checks that the profile plane contains the axis). An
exception inside a feature becomes an error record on that feature with its
source line and a message written for a reader who will fix the line. The
body passes through unchanged.

### The sketch solver

Variables: the coordinates of every non-fixed, non-projected entity. Each
constraint contributes residuals; each dimension contributes one. Solve with
`least_squares` and analytic Jacobians. Degrees of freedom equal variables
minus Jacobian rank; a rank deficiency with a non-zero final residual reports
over-constraint and names the redundant constraints.

Dragging is not a soft residual: a weak pull the constraints forbid settles
at a compromise whose small violations tilt other entities. Each drag
iteration takes a constrained tangent step toward the target (one KKT solve)
and then projects back onto the constraints with a Gauss-Newton least-norm
step. A fully constrained sketch does not move at all. Solving never writes
the file: the GUI previews solutions through the solve endpoint and commits
by issuing a write-back edit.

### The selector language

A selector is a chain over `faces`, `edges` or `vertices` of a feature or
instance:

- semantic: `.top`, `.bottom`, `.start`, `.end`, `.inner` and
  `.from_sketch("rect1.top")`, resolved through the identity map against the
  entities the named feature owns; steps chain (`.top.from_sketch("rect1.right")`
  is one edge)
- predicates: `.where(normal="+Z")`, `.where(kind="cylinder")`,
  `.where(parallel_to="+X")`
- reducers: `.nearest(point)`, `.largest()`, `.smallest()`, `.all()`

On an assembly instance the same chains apply in the part's own coordinates,
`.of("feature")` narrows to what one feature of the part made, and
`inst.planes.XY` and `inst.axes.Z` name the part's standard planes and axes.
A bare feature name in a render highlight or a measurement means everything
the feature owns on the final body. The parser rejects integer indexing.

### The server API

All under `/api`. Responses are JSON unless noted; errors are `{error}` with
400 for a failed operation, 404 for an unknown document, 409 for a stale hash
or an existing file, 422 for a malformed request.

```
GET    /health                                 root, pid, port, tabs, idle minutes
POST   /shutdown                               stop the server
GET    /documents                              open documents with their kind
GET    /files                                  model and STEP files under the project
POST   /documents/open       {path}            a .py, a .step which gets a wrapper, or .step#node for one sub-assembly of it
POST   /documents/{id}/close
POST   /documents/new        {path, kind, name, material, of}
POST   /documents/import     {source, folder, name}   copy a STEP file from this machine into the project and open it
PUT    /documents/upload?folder&name&suffix    the same from the bytes of a dropped file
POST   /open-request         {path}            tell the open tabs to open a project file or to import a STEP file; how many heard
GET    /documents/{id}/tree                    features with results, constraints, planes, dependants,
                                               names for expressions, the geometry revision; a drawing's
                                               evaluation carries the whole sheet as a 2D scene
GET    /documents/{id}/source                  text and hash
PUT    /documents/{id}/source {source, hash}   the code pane's writes
POST   /documents/{id}/edit   {op, ..., hash}  one operation or a batch; sketch ops solve and write back
POST   /documents/{id}/undo | /redo
POST   /documents/{id}/solve  {sketch, drag}   a sketch solution, no write
POST   /documents/{id}/preview {op, choose_flip}   the document with an edit applied, no write: poses and verdicts
POST   /documents/{id}/preview-mesh {op}       the same as a mesh, for a dialog's live preview
POST   /documents/{id}/drag   {instance, poses, translate, rotate}   one step of dragging an instance
GET    /documents/{id}/mesh?upto&tolerance&plane&offset&flip   binary mesh
GET    /documents/{id}/mesh?overlay=OTHER | rev=REV   the comparison as a mesh, the report in the header
GET    /documents/{id}/query/{kind}            volume, area, bbox, counts, center_of_mass, mass, summary,
                                               bom and interference
POST   /documents/{id}/measure {a, b, section} an entity description or a pair measurement
POST   /documents/{id}/locate {refs, upto}     the mesh ids a selector or feature name picks
POST   /documents/{id}/render {view, width, height, highlight, section, overlay, rev}   PNG
POST   /documents/{id}/compare {other, rev, upto}   volumes and regions added and removed
POST   /documents/{id}/export {format, path}   step, stl; a drawing: svg, dxf, pdf
GET    /documents/{id}/views | PUT {views}     the sidecar view state
WS     /documents/{id}/events                  hello, changed, external, dependency
WS     /events                                 one per tab: hello, open-request
```

The mesh is one binary buffer: a uint32 header length, a JSON header padded
to four bytes, then float32 positions and normals, uint32 indices, uint32 face
ids, and edge and vertex positions with per-entity ranges. The header carries
per-face triangle ranges, the label and tag maps, per-item ranges and
colours, every face's centre and every edge's half-way point (what a click
names through `nearest()`), and the bounding box. OCCT triangulates each face
separately, so the face id is exact per vertex.

### Edit operations

`set_argument`, `set_parameter`, `add_parameter` (after the last parameter,
so every feature can use it), `delete_parameter` (refused while used),
`set_meta`, `add_feature` (composed from kind and arguments, or a literal
statement), `delete_feature` (cascade by default), `add_sketch_entity`,
`delete_sketch_entity`, `set_entity_argument`, `add_constraint`,
`delete_constraint`, `set_constraint_value`, `solve_sketch`, `write_back`,
`write_poses`, `batch`, `replace_source`, and two that need the evaluation:
`explode_import` (a review import becomes one instance per body, posed as the
file placed it) and `make_editable` (a vendor body gets a part file). On an
assembly document every operation but `replace_source` solves the mates and
writes the poses back in the same commit. Writing any document re-evaluates
the open documents that depend on it. The contracts are pydantic models in
`operations.py`; `scripts/generate_edit_types.py` turns them into the
browser's types.

### The CLI

```
plainsolid serve [dir] --open FILE --port N --idle-exit MIN   dir: cad/ in the checkout by default; created if missing
plainsolid open FILE...                  in the running app, or a new server; a STEP file from outside goes through the import dialog
plainsolid status | stop                 the server on the port
plainsolid install-launcher              "Open in plainsolid" in the Finder, or a desktop entry on Linux
plainsolid new FILE [--kind assembly | --kind drawing --of MODEL] [--name N] [--material M]
plainsolid tree FILE                     also accepts a .step file
plainsolid query FILE KIND [--upto F]
plainsolid measure FILE REF [REF] [--plane P --offset D --flip]
plainsolid edit FILE OP_JSON [--dry-run]
plainsolid render FILE -o out.png --view V --size WxH [--highlight SELECTOR|FEATURE]... [--plane ...] [--overlay OTHER | --rev REV]
plainsolid compare FILE [OTHER | --rev REV] [-o overlay.png]
plainsolid export FILE -o out.step|out.stl     a drawing: out.pdf|out.dxf|out.svg
plainsolid mesh FILE -o out.bin [--plane ...]
plainsolid docs                          the agent guide
plainsolid mcp [dir]                     the engine as MCP tools on stdio; dir as for serve
```

### Repository layout

```
plainsolid/
  pyproject.toml, uv.lock
  src/plainsolid/     the engine, edit layer, workspace, server, CLI, MCP server, agent guide
  web/                the browser client (Vite, React, three.js); its README describes it
  zoo/                reference documents; also the test fixtures
  tests/              the Python suites, perf_baseline.json
  scripts/            bench.py, generate_edit_types.py
  docs/               this document, testing.md, images
  corpus/             real vendor and manufacturer files, local only, never committed
  cad/                your documents: the default project, gitignored
  .github/workflows/  CI
```

## The zoo

Reference documents that double as test fixtures and documentation examples.
Each has a sibling `.expected.json` with golden values.

| Document | Exercises |
|---|---|
| `zoo/bracket.py` | L profile, two slots, two holes placed from projected edges, inner fillet, outer chamfer, a parameter driving the slot spacing |
| `zoo/enclosure.py` | An outdoor box: a rounded rectangle extruded and shelled, a tongue around the rim by a rebate cut from projected edges, corner bosses and board standoffs by two-direction patterns with pilot holes, gland and antenna holes on the walls placed from projected floor edges and cut up to the inner faces, a row of vent slots by a linear pattern |
| `zoo/lid.py` | A rounded plate with an outer skirt and an inner rim under it (the enclosure's tongue seats between them), four screw holes by a pattern, chamfered top edges |
| `zoo/board.py` | A circuit board: a rounded plate with four mounting holes by a pattern, a sensor module and a connector as blocks on top |
| `zoo/vendor/gen_vendor.py` | Generator for the vendor stand-ins as one coloured solid each: an M12 gland (thread, hex flange, domed cap nut), a stub antenna (SMA nut, rod, rounded tip) and a framed solar panel with its junction box (`gen_node_stub.py` makes the review assembly) |
| `zoo/node.py` | An outdoor sensor node: the enclosure, its lid and the board on the standoffs inside, the bracket, two glands, the antenna and the panel with fixed, coincident, concentric, distance and angle mates; fully constrained but for the round parts' spin |
| `zoo/node_review.py` | The review import of the stub proposal: a STEP viewer document |
| `zoo/bracket_dwg.py` | Drawing of the bracket: front, top, right, an isometric at 3:4 and a section through a hole, nine dimensions (distances, a diameter, a radius, the chamfer angle), a note and the title block |

## Out of scope

Renaming with cascade, multi-body parts, sheet metal, nested assemblies,
in-context editing, wall thickness at a point, authentication, Windows,
configurable key bindings, variable-radius fillets, splines, bill of materials
tables in drawings, detail views and tolerances, simplified representations
for heavy vendor parts, a command palette, a history list.
