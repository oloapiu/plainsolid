# plainsolid web client

The browser UI. It is a strict client of the local API server: it holds no model state beyond what
the API returns, and every change it makes is an edit operation the server applies to the model
file. View state (camera, section, pins, visibility, named views) goes to the `<doc>.views.json`
sidecar through the views API, never into the model file.

## Run and build

    # in one terminal, from the repo root
    uv run plainsolid serve zoo --open bracket.py --open node_review.py --port 8321
    # in another
    cd web && npm install && npm run dev        # http://127.0.0.1:5199, proxies /api to 8321

- The dev proxy targets `http://127.0.0.1:8321`. Set `PLAINSOLID_API=http://127.0.0.1:8330` to point
  it at a server on another port.
- `npm run build` type-checks and writes the bundle to `../src/plainsolid/static`. The Python server
  serves it at `/`, so `uv run plainsolid serve …` alone runs the whole app after a build.
- Edit types in `src/api/operations.ts` are generated from the Python contract: after changing it,
  run `uv run python scripts/generate_edit_types.py` from the repository root.
  `tests/test_contracts.py` checks that the file is current.

## Tests

- `npm test` runs `tests/state.test.mjs` under `node --test`. It loads the real store modules in a
  `node:vm` context with scripted network replies and timers, no DOM or CAD kernel, and covers save
  ordering, conflicts, closing, tab switches, late replies and preview coalescing.
- `node verify.mjs <url> <project dir>` drives the client in Playwright's Chromium and prints PASS or
  FAIL per check. It edits the files, so the server must serve a scratch copy of `zoo/` with
  `bracket.py`, `node_review.py` and `node.py` open:

      P=$(mktemp -d) && cp -r zoo/* $P/
      uv run plainsolid serve $P --port 8399 &
      for d in bracket.py node_review.py node.py; do
        curl -s -X POST http://127.0.0.1:8399/api/documents/open -H 'Content-Type: application/json' -d "{\"path\":\"$d\"}" > /dev/null
      done
      cd web && node verify.mjs http://127.0.0.1:8399/ $P

- The `browser` job in `.github/workflows/ci.yml` runs that recipe against the built bundle on every
  push and fails on any FAIL line; the `test` job runs `npm test` and the build.

## Debugging hooks

`src/main.tsx` exposes `window.__plainsolid` for `verify.mjs` and the console: `getState()` (the
store, read-only); `clipCount()`, `planeCount()`, `isOrtho()`, `hasGhost()`, `pixelSize()` (what the
scene draws); `codeCursorTo(line)`; `planePointAt(x, y)`, `sketchToScreen(u, v)`, `sketchHit(u, v)`;
and `actions`: `pickEntity(kind, id)`, `selectorFor(kind, id)`, `entityCenter`, `useBodyInRelation`,
`dragInstance(dx, dy)` and the store's own (`setSection`, `edit`, `openDocument`, `addMate`, …).

## Source layout

    src/api        typed client, mesh parser (items, vertices, section caps, reference points), WebSocket, generated edit types
    src/state      the store: core, documents, geometry, requests, tools, sketch and views modules behind the store.ts facade
    src/viewport   three.js scene (meshes, visibility, clipping, picking, labels, triad, camera), heads-up bar, section and move panels
    src/tree       the feature tree of parts, assemblies and drawings; the instance tree of a STEP viewer
    src/panel      property panel, feature and plane dialogs, pick fields, expression input, measure panel
    src/params     the parameter list with its add row
    src/code       the CodeMirror pane
    src/sketch     sketch mode: 2D model (model.ts), three.js drawing (draw.ts), the overlay (tools, selection, drag), HTML labels
    src/drawing    the sheet of a drawing document
    src/menu       the right-click menu and its entry builders
    App.tsx, FilesMenu.tsx, Menu.tsx, Help.tsx: the frame, the open and new popovers, the drop-down, the keys card

See [src/state/README.md](src/state/README.md) for module ownership and request-lifetime rules.

## Documents

- Open documents are tabs in the top bar, each with a close button (middle-click closes too).
- "open ▾" lists the project's documents, recent first, then parts, assemblies, drawings and STEP
  files, with a filter; enter opens the first match or a typed path. A STEP file opens as a viewer.
- "new ▾" creates a part, an assembly or a drawing (asking for its model) next to the current one.
- "file ▾" holds snapshot (a PNG, saved and copied to the clipboard), export (STEP for a part or
  assembly; PDF, DXF or SVG for a drawing, by suffix) and the compare commands.
- The code pane marks the selected feature's lines and error lines; the cursor on a feature's line
  selects it, ctrl+click on a name jumps to its definition, and the header drags to resize (120 to
  600 px). Typing saves after a short pause; the header shows saved, unsaved, saving…, save failed
  or conflict. A failed save keeps the draft and blocks closing or switching away. If the file changed
  on disk while you typed, the pane offers "reload file" or "use my version"; typing never overwrites.
- The server pushes changes over a WebSocket: an external edit reloads the document, and a change to
  a part that an assembly instances or a drawing shows refreshes it (`dependency` event).

## Viewport and camera

- The camera is a trackball: dragging turns about the screen axes without limit and the up vector
  follows; the standard views and "normal to" straighten it. Right drag or shift+middle drag pans,
  the wheel zooms about the cursor. There is no floor grid.
- The heads-up bar at the bottom-right holds iso, front, top, right, fit and ortho; section, measure,
  move (assemblies) and planes (the standard planes and plane features as pickable squares); and
  the named views. An axis triad in the bottom-left corner turns with the camera; a tip sets a view.
- Faces and edges pick everywhere: hovering lights them and names the entity, its owner and identity
  tags; a click selects. The edge test runs only over the item under the cursor. Vertices are drawn
  and pickable in measure mode, sketch mode and dialog fields that take them.
- Right-click menus everywhere (`src/menu/entries.ts`): the tree, body geometry, empty space, a
  sketch and the sheet get their own entries, the active tool adds its own, and a right-click
  selects what it lands on.
- Both side panels have a splitter (the tree 170 to 480 px, the panel 220 to 720 px) and a corner
  toggle that collapses them to an 18 px strip. On a small window the panels give way so the
  viewport keeps 420 px. Widths and collapsed states are remembered per browser.

## Parts

- Tree rows show the feature's icon, name and defining value (`R3`, `profile · 40`, `XY + 10`,
  `3 × 24`); the kind is in the tooltip, badges mark read-only, suppressed and failing features.
  Hovering a row lights its geometry and hovering geometry tints its row; a button rolls back to it.
- The header offers "+ plane", "+ sketch" and "+ feature" (fillet, chamfer, shell, mirror body).
  Patterns, mirrors and revolves start from the feature or sketch they act on.
- "+ sketch" puts the sketch on the selected face, on the selected plane feature, or asks for XY, XZ,
  YZ or a plane feature. "+ plane" has four forms: offset from a plane or face, angle about an edge
  on a base, midplane between two faces or planes, through three vertices.
- Dialog pick fields arm as the dialog opens and take the face or edge selected beforehand; multi-pick
  fields stay armed until escape. The extrude, cut, fillet, chamfer, shell, revolve, pattern and
  mirror dialogs preview the result over the faded body as the fields change (a server mesh with the
  edit applied, nothing written); "add" writes the feature and closes the sketch it started from.
  Previews are coalesced: one request in flight, only the newest change waits.
- The selector a click writes is semantic when the identity map makes it unique (`body.faces.top`,
  `boss.edges.top.from_sketch("c")`), else `body.faces.nearest((x, y, z))` at the server's reference point.
- A selected feature's panel edits its arguments. Hovering a reference lights what it picks (through
  `POST /locate`); a reference the finished body no longer has lights the feature holding it. A
  selected edge is described (owner, tags, length) with fillet and chamfer buttons.
- With nothing selected the panel shows the size line (mass, volume, box, faces), the meta fields
  (material, revision, author), the problems (a click selects the feature) and the parameters with
  an add row and a delete cross; a parameter still in use refuses to go. Numeric fields take
  expressions with autocomplete, refuse unknown names before writing, and step with the arrow keys.
- Deleting a feature with dependants asks first and lists what goes; the server cascades so the file
  never references an undefined name. Undo brings everything back.

## Sketching

- Editing a sketch rolls the model back to the feature before it, switches to orthographic projection
  and looks at the plane; the finished body is drawn faint behind. Exiting restores both.
- Entities draw from the solver's coordinates: blue while they have degrees of freedom, white when
  fully constrained, red when a conflicting constraint touches them; construction and converted
  geometry dashed. The bar shows the degrees of freedom and names redundant or conflicting constraints.
- Tools: line (chained), circle, arc, rect, slot, polygon, point; escape stops the tool, never the sketch.
- Snapping: ends, centres, corners, midpoints and points on a curve. A glyph at the cursor names what
  the next click writes (• coincident, ⊣ midpoint, ⊙ on curve, — horizontal, | vertical, ◠ tangent
  to the arc the line leaves); the inferred constraints go in the same commit as the entity.
- Select by clicking handles or curves, shift-click for up to three. The context bar lists the
  constraints valid for the selection and applies one per click. `d` starts a dimension: click to
  place it, a prefilled value field opens, enter commits (point-point offers `dist`, `dx`, `dy`).
  Dimension labels drag to place and click to edit; alt-click on a constraint glyph deletes it.
- Drag a handle or an entity (a circle's curve changes its radius): `POST /solve` previews at about
  30 Hz latest-wins, release commits `solve_sketch`; a fully constrained entity stays and says "locked".
- Convert (`e`) writes `project` entities that follow body edges, vertices or a face outline. Without
  a tool, a plain click on the body selects a face, edge or vertex and the bar offers "convert" and
  "convert as construction"; shift+click on an edge or vertex converts it at once and selects it.
- Offset writes an offset of the selected curves (or the whole profile) on the side clicked.
  Construction: `x` flips the selection in one commit; the dashed bar button (shift+x) draws it.
- The sketch panel lists entities and constraints with editable values and delete buttons.

## Assemblies

- The tree header shows a mode chip (part, assembly, STEP viewer, drawing) and the name. An assembly
  adds a status chip, "fully constrained", "3 free" or "2 unsatisfied", with details on demand.
- Rows list features in file order: instances with a colour swatch, visibility checkbox (alt+click
  isolates), transparency toggle and `fixed` or `free` badges; mates with a warning badge when the
  solver finds them redundant or unsatisfied. Instances into one STEP file are grouped by path.
- Clicking a face selects its instance. The instance panel shows file, product, colour, material,
  pose (`at`, `rotate`, editable; the solved pose underneath), how the mates hold it, "fix here",
  and "open part" (a part file) or "edit part" (a STEP body of one solid: the server writes a part
  file wrapping it, repoints every instance of the same product, and the part opens in a tab).
- A STEP viewer is what opening a STEP file gives: the file's hierarchy and "make editable", which
  turns the bodies into instances of an assembly, still read from the STEP file.
- "+ instance" asks for a file (the project's parts and STEP files are suggested and written
  relative to the assembly), an optional colour and material, and names the instance after the file.
- "+ mate" arms the first pick at once (a face selected beforehand is the first reference), the
  second follows, and escape hands over to the "pick" buttons; a reference can also be an
  instance's standard plane or axis. The kind follows the picks (two round entities are concentric)
  until chosen by hand. The assembly previews the mate as kind, value and flip change; a new pair is
  tried both ways and the flip that turns the parts less is chosen. Add writes the mate and the poses.
- The move tool (`m` slide, `r` turn) drags an instance along what its mates leave free; the server
  re-solves and release writes the poses. A fully constrained part does not move and the status says why.
- With nothing selected the panel shows the solver state, the bill of materials (one row per part
  file with count, material and mass, and the total) and "check interference".
- A change to an instanced part file refreshes the assembly through the `dependency` event. "export
  STEP" writes the posed assembly with its instance names and colours.

## Drawings

- A drawing replaces the viewport with the sheet: an SVG of the scene the server laid out in sheet
  millimetres (views, section traces, dimensions, notes, frame, title block). Wheel zooms, drag pans.
- Clicking a view, dimension or note selects it in the tree and opens its panel. Dragging one moves
  it and rewrites its `at` on release (views on the sheet; dimensions and notes relative to their
  view, so a view carries them along). The tree lists them with what they show (`front`,
  `section XZ`, `distance 60`); "open model" opens the model in a tab and the drawing follows it.
- "+ view" offers the seven directions and "section…" (a plane and an offset). "+ dimension" makes
  the sheet a pick tool: edges the server labelled with a selector are hit targets; pick one or two,
  then click where the dimension goes. "+ note" asks for the text and places it at the click.
- Panels edit a view's direction or section plane, offset and side, place, scale and hidden lines; a
  dimension's kind, along axis, text override and place; a note's text, size, view and place. With
  nothing selected the panel sets the sheet size (A4, A3, letter), the scale and the title block
  fields (each a `set_meta` edit of the `meta()` line) and exports PDF, DXF or SVG.

## Review tools

- Section: plane XY, XZ or YZ, an offset slider and field, flip. The slider clips on the GPU while
  dragging; a commit is debounced 150 ms and fetches the server's capped mesh (caps red), the clip
  staying on until it is installed. Fetches are latest-wins. It applies to measurements and snapshots.
- Measure: click faces, edges or vertices, or type `face:3`, `edge:5`, `vertex:2` or `0,0,0`. Results
  show distance with components, angle, and each entity's type and size. Pin keeps a label in the scene.
- Named views (the `views…` drop-down) save, restore and delete camera, section, pins and
  visibility; the last camera and section come back on load. Snapshot includes pins and the section.
- Compare: "compare with…" overlays another document of the project, "compare with last commit" the
  file as git last saw it: green is what this document adds, red what it lacks, grey what both share.
  The panel reports volumes and changed regions and the overlay follows every edit; the section is off.
- Isolate: alt+click on a visibility checkbox, or "isolate" in a menu, shows one instance; "show
  all" brings the rest back.

## Keys

The `?` card (`src/Help.tsx`) lists them in the app.

- Everywhere: ctrl+z, ctrl+shift+z undo and redo; ctrl+o open; ctrl+tab, ctrl+], ctrl+[ cycle tabs;
  ctrl+` show or hide the code pane; delete or backspace removes the selected feature, instance,
  mate, view, dimension or note; esc cancels a pick, a tool or a dialog, else clears the selection.
- Viewport: drag orbits, right drag pans, wheel zooms, triad tips set a view; ctrl+1 to ctrl+7 front,
  back, left, right, top, bottom, iso; f fits; ctrl+0 looks straight at the selected face;
  shift+click adds to a dialog's picks.
- Assemblies: m and r are the move tool's slide and turn; alt+click on a visibility checkbox isolates.
- Sketch: l c a r s p o tools; d dimension; e convert; x and shift+x construction; shift+click adds
  to the selection (on a body edge or vertex, relates to it); shift held draws without snapping;
  alt+drag or middle drag orbits (a left drag on empty space too); shift+left drag pans; ctrl+0
  normal to; enter closes a polygon; delete removes the selection; esc stops the tool, then clears.
- Drawing: drag moves a view, dimension or note; esc stops the dimension or note tool.
- Fields: ↑ ↓ step a number by 1 (shift 10, alt 0.1); tab accepts a completion; ctrl+s in the code
  pane saves now.

## Known gaps

- Vertices pick only in measure mode, sketch mode and dialog fields that take them.
- Pins reference mesh ids, so they go stale when the geometry changes underneath.
- A polygon's points are not editable in the panel; the code pane's undo is separate from the server's.
- On a sheet only edges the server labelled with a selector are pickable for dimensions.
- Selecting an edge shows its length, but the distance between two faces needs the measure tool.
- Dimension label placements live in `localStorage` per document and sketch, not in the views
  sidecar, because the server keeps only known keys in `<doc>.views.json`.
- Constraint glyphs are HTML chips near the first reference, not icons on every entity.
