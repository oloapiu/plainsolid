# Client state

UI components import the public API from `store.ts`. This facade preserves the
existing imports while each implementation module owns a specific responsibility:

- `core.ts`: state types, the single state value, subscriptions and busy tracking.
- `documents.ts`: opening/switching/closing, source drafts, saves, edits and history.
- `geometry.ts`: meshes, comparisons, derived queries and revision checks.
- `requests.ts`: document sessions, request scopes, cancellable request streams and
  `Coalesced`, the latest-wins wrapper feature and mate previews run through.
- `tools.ts`: selection, feature and assembly tools, drawing and measurement actions.
- `sketch.ts`: sketch mode, constraints and solver previews.
- `views.ts`: camera and view-sidecar persistence.

Only `core.ts` replaces the state value. Modules call `set` and share its live
binding; they never create a second store. Calls between document and tool modules
happen inside functions, after module initialization. Keep those imports explicit;
implementation modules must not import the facade or invoke each other's actions
at module scope.

A successful tab transition advances the document session. A blocked transition
leaves the session and its subscriptions intact. `RequestLane` cancels obsolete
work and ignores late replies even if the user revisits the same document.
`requestScope(true)` additionally checks the geometry revision and rollback target.
Use a geometry scope for previews/queries, and compare mesh payload revisions
before installation. Document loading can discover a newer revision, so it uses
a session scope rather than pinning the old geometry revision.

Drafts keep their own base source and hash. Geometry refreshes must not advance a
dirty draft's base. Saves drain in order per document; closing or switching awaits
them. The regression suite in `web/tests/state.test.mjs` exercises the real modules
with controlled network replies and timers, without a DOM or CAD fixtures.
