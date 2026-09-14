// Run the real TypeScript store with deterministic network replies and timers.
// No DOM or CAD kernel is needed to exercise request ordering and save failures.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle(predicate) {
  for (let i = 0; i < 50; i++) { if (predicate()) return; await tick(); }
  assert.ok(predicate(), 'request did not settle');
}

async function fixture() {
  let clock = 0;
  const timers = new Map();
  const context = vm.createContext({
    AbortController, performance, console,
    setTimeout: (fn, ms) => { const id = ++clock; timers.set(id, { fn, ms }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  const docs = {
    a: { source: 'original A', hash: 'a1', revision: 'a-geometry-1' },
    b: { source: 'original B', hash: 'b1', revision: 'b-geometry-1' },
  };
  const calls = [], events = new Map();
  class ApiError extends Error {
    constructor(status, message, hash) { super(message); this.status = status; this.hash = hash; }
  }
  const update = (id, source, revision) => {
    docs[id].source = source; docs[id].hash += '+'; docs[id].revision = revision ?? docs[id].revision + '+';
  };
  const api = {
    getViews: async () => ({}),
    putViews: async () => ({}),
    listDocuments: async () => Object.keys(docs).map(id => ({ id })),
    tree: async id => ({ id, hash: docs[id].hash, revision: docs[id].revision, kind: 'assembly', path: id,
      features: [], params: [], errors: [], evaluation: { seconds: 0, instances: [] } }),
    source: async id => ({ ...docs[id] }),
    mesh: async id => { calls.push(['mesh', id]); return { header: { hash: docs[id].hash, revision: docs[id].revision } }; },
    query: async (id, kind) => { calls.push(['query', id, kind]); return { revision: docs[id].revision, rows: [], volume: docs[id].revision }; },
    putSource: async (id, text, hash) => {
      calls.push(['save', id, text]);
      if (hash !== docs[id].hash) throw new ApiError(409, 'stale', docs[id].hash);
      update(id, text);
      return { hash: docs[id].hash, changed: true };
    },
    edit: async id => { calls.push(['edit', id]); update(id, 'edited'); return { hash: docs[id].hash, changed: true }; },
    closeDocument: async id => { calls.push(['close', id]); delete docs[id]; return { documents: Object.keys(docs).map(id => ({ id })) }; },
    events: (id, cb) => { events.set(id, cb); return () => events.delete(id); },
  };
  const modules = new Map();
  const synthetic = (name, values) => new vm.SyntheticModule(Object.keys(values), function () {
    for (const [key, value] of Object.entries(values)) this.setExport(key, value);
  }, { context, identifier: name });
  modules.set('react', synthetic('react', { useSyncExternalStore() { throw Error('React rendering is outside this unit test'); } }));
  modules.set('api', synthetic('api', { api, ApiError }));
  // Discover modules before linking so the store's function-only module cycles
  // use Node's normal ESM linker, rather than recursively starting separate links.
  async function discover(url) {
    if (modules.has(url)) return;
    const source = await readFile(new URL(url), 'utf8');
    const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
    const module = new vm.SourceTextModule(compiled.outputText, { context, identifier: url });
    modules.set(url, module);
    for (const specifier of module.dependencySpecifiers) {
      if (specifier === 'react' || specifier.endsWith('/api/client')) continue;
      await discover(new URL(specifier + '.ts', url).href);
    }
  }
  const url = new URL('../src/state/store.ts', import.meta.url).href;
  await discover(url);
  const module = modules.get(url);
  await module.link((specifier, parent) => {
    if (specifier === 'react') return modules.get('react');
    if (specifier.endsWith('/api/client')) return modules.get('api');
    return modules.get(new URL(specifier + '.ts', parent.identifier).href);
  });
  await module.evaluate();
  const store = module.namespace;
  return { store, api, docs, calls, events, update, ApiError, timers };
}

test('failed saves preserve the draft, tab and its event subscription', async () => {
  const { store: s, api, docs, events } = await fixture();
  await s.useDocument('a');
  api.putSource = async () => { throw Error('offline'); };
  s.sourceEdited('draft A');
  assert.equal(await s.useDocument('b'), false);
  assert.equal(s.getState().docId, 'a');
  assert.equal(s.getState().source, 'draft A');
  assert.equal(s.getState().saveState, 'error');
  assert.equal(s.getState().error, 'offline');
  docs.a.revision += '+';
  events.get('a')({ event: 'dependency', hash: docs.a.hash, revision: docs.a.revision });
  await settle(() => s.getState().revision === docs.a.revision);
  assert.equal(s.getState().source, 'draft A');
});

test('closing saves the draft before removing the document', async () => {
  const { store: s, calls } = await fixture();
  await s.useDocument('a'); s.sourceEdited('draft A');
  await s.closeDocument('a');
  assert.deepEqual(calls.filter(c => ['save', 'close'].includes(c[0])), [['save', 'a', 'draft A'], ['close', 'a']]);
  assert.equal(s.getState().docId, 'b');
  assert.equal(s.getState().source, 'original B');
});

test('closing after a failed save leaves the document open and retryable', async () => {
  const { store: s, api, docs, calls } = await fixture();
  await s.useDocument('a'); s.sourceEdited('draft A');
  const put = api.putSource;
  api.putSource = async () => { throw Error('disk full'); };
  await s.closeDocument('a');
  assert.ok(docs.a);
  assert.equal(s.getState().source, 'draft A');
  assert.ok(!calls.some(c => c[0] === 'close'));
  api.putSource = put;
  assert.equal(await s.flushSource(), true);
  assert.equal(docs.a.source, 'draft A');
  assert.equal(s.getState().saveState, 'saved');
});

test('typing during a save drains the newest text in order, with no overlapping writes', async () => {
  const { store: s, api, docs, calls } = await fixture();
  await s.useDocument('a');
  const gate = deferred(), put = api.putSource;
  let active = 0, peak = 0;
  api.putSource = async (...args) => {
    active++; peak = Math.max(peak, active);
    await gate.promise;
    try { return await put(...args); } finally { active--; }
  };
  s.sourceEdited('first');
  const first = s.flushSource();
  s.sourceEdited('second');
  const second = s.flushSource();
  gate.resolve();
  assert.deepEqual(await Promise.all([first, second]), [true, true]);
  assert.equal(peak, 1);
  assert.equal(docs.a.source, 'second');
  assert.deepEqual(calls.filter(c => c[0] === 'save').map(c => c[2]), ['first', 'second']);
  assert.equal(s.getState().codeDirty, false);
});

test('a changed file produces a conflict; further typing does not overwrite it', async () => {
  const { store: s, docs, calls, update } = await fixture();
  await s.useDocument('a'); s.sourceEdited('my draft');
  update('a', 'external work');
  assert.equal(await s.flushSource(), false);
  assert.equal(s.getState().saveState, 'conflict');
  assert.equal(s.getState().source, 'my draft');
  const before = calls.length;
  s.sourceEdited('my newer draft');
  assert.equal(await s.flushSource(), false);
  assert.equal(calls.length, before);
  assert.equal(docs.a.source, 'external work');
  await s.overwriteSource();
  assert.equal(docs.a.source, 'my newer draft');
  assert.equal(s.getState().saveState, 'saved');
});

test('watcher refetch never adopts the external hash as the draft base', async () => {
  const { store: s, docs, events, update } = await fixture();
  await s.useDocument('a'); s.sourceEdited('my draft');
  const baseHash = s.getState().sourceHash;
  update('a', 'external work');
  events.get('a')({ event: 'external', hash: docs.a.hash, revision: docs.a.revision });
  await settle(() => s.getState().saveState === 'conflict');
  assert.equal(s.getState().sourceHash, baseHash);
  assert.equal(await s.flushSource(), false);
  assert.equal(docs.a.source, 'external work');
  await s.reloadSource();
  assert.equal(s.getState().source, 'external work');
  assert.equal(s.getState().saveState, 'saved');
});

test('an empty draft cannot close a document; reload recovers the saved file', async () => {
  const { store: s, docs } = await fixture();
  await s.useDocument('a'); s.sourceEdited('');
  await s.closeDocument('a');
  assert.ok(docs.a);
  assert.equal(s.getState().source, '');
  assert.equal(s.getState().saveState, 'error');
  await s.reloadSource();
  assert.equal(s.getState().source, 'original A');
});

test('a dependency event refreshes meshes and derived queries without a source change', async () => {
  const { store: s, docs, calls, events } = await fixture();
  await s.useDocument('a'); await s.fetchAssemblyQueries(); await s.fetchSummary();
  const sourceHash = docs.a.hash;
  docs.a.revision += '+';
  calls.length = 0;
  events.get('a')({ event: 'dependency', hash: docs.a.hash, revision: docs.a.revision });
  await settle(() => s.getState().mesh?.header.revision === docs.a.revision);
  await s.fetchAssemblyQueries(); await s.fetchSummary();
  assert.equal(s.getState().hash, sourceHash);
  assert.ok(calls.some(c => c[0] === 'mesh'));
  assert.equal(s.getState().queries.revision, docs.a.revision);
  assert.equal(s.getState().summary.revision, docs.a.revision);
});

for (const dependencyOnly of [false, true]) test(`reconnect catches ${dependencyOnly ? 'dependency' : 'source'} changes`, async () => {
  const { store: s, docs, events, update } = await fixture();
  await s.useDocument('a');
  if (dependencyOnly) docs.a.revision += '+';
  else update('a', 'changed while disconnected');
  events.get('a')({ event: 'hello', hash: docs.a.hash, revision: docs.a.revision });
  await settle(() => s.getState().mesh?.header.revision === docs.a.revision);
  assert.equal(s.getState().source, docs.a.source);
});

test('a late views response cannot overwrite the newer tab', async () => {
  const { store: s, api } = await fixture();
  const gate = deferred();
  api.getViews = id => id === 'a' ? gate.promise : Promise.resolve({ named: { B: {} } });
  const old = s.useDocument('a');
  await tick(); await s.useDocument('b');
  gate.resolve({ named: { A: {} } }); await old;
  assert.equal(s.getState().docId, 'b');
  assert.deepEqual(Object.keys(s.getState().named), ['B']);
});

test('late tree/source responses cannot overwrite the newer tab', async () => {
  const { store: s, api } = await fixture();
  await s.useDocument('a');
  const gate = deferred(), tree = api.tree;
  api.tree = async id => { const result = await tree(id); if (id === 'a') await gate.promise; return result; };
  const old = s.refetch();
  await s.useDocument('b'); gate.resolve(); await old;
  assert.equal(s.getState().tree.id, 'b');
  assert.equal(s.getState().source, 'original B');
});

test('an edit completing in the old tab cannot change the active hash', async () => {
  const { store: s, api, docs } = await fixture();
  await s.useDocument('a');
  const gate = deferred(), edit = api.edit;
  api.edit = async (...args) => { await gate.promise; return edit(...args); };
  const old = s.edit({ op: 'set_meta', key: 'revision', value: 'B' });
  await tick(); await s.useDocument('b'); gate.resolve(); await old;
  assert.equal(s.getState().hash, docs.b.hash);
  assert.equal(s.getState().source, docs.b.source);
});

test('a mismatched mesh revision refetches its tree before installing it', async () => {
  const { store: s, api, docs } = await fixture();
  const mesh = api.mesh;
  let first = true;
  api.mesh = async id => { if (first) { first = false; docs[id].revision += '+'; } return mesh(id); };
  await s.useDocument('a');
  assert.equal(s.getState().revision, docs.a.revision);
  assert.equal(s.getState().mesh.header.revision, docs.a.revision);
});

test('stale hash with unchanged source is retried without a false conflict', async () => {
  const { store: s, docs } = await fixture();
  await s.useDocument('a'); s.sourceEdited('draft');
  docs.a.hash += '+';
  assert.equal(await s.flushSource(), true);
  assert.equal(docs.a.source, 'draft');
  assert.equal(s.getState().saveState, 'saved');
});

test('the editor is locked for close and unlocked if closing fails', async () => {
  const { store: s, api } = await fixture();
  await s.useDocument('a');
  const gate = deferred();
  api.closeDocument = async () => { await gate.promise; throw Error('close failed'); };
  const closing = s.closeDocument('a');
  await tick();
  assert.equal(s.getState().closingDoc, 'a');
  s.sourceEdited('cannot edit during close');
  assert.equal(s.getState().source, 'original A');
  gate.resolve(); await closing;
  assert.equal(s.getState().closingDoc, null);
  s.sourceEdited('can edit again');
  assert.equal(s.getState().source, 'can edit again');
});

for (const kind of ['feature', 'mate']) test(`a late ${kind} preview cannot restore an old session after revisiting its tab`, async () => {
  const { store: s, api } = await fixture();
  await s.useDocument('a');
  const gate = deferred();
  api.previewMesh = async () => { await gate.promise; throw Error('old feature failed'); };
  api.preview = async () => { await gate.promise; return { ok: true, poses: {} }; };
  const old = kind === 'feature' ? s.previewFeature({ op: 'noop' }) : s.previewMate('fixed', 'part', '');
  await s.useDocument('b'); await s.useDocument('a');
  gate.resolve(); await old;
  assert.equal(s.getState().previewNote, null);
  assert.equal(s.getState().posePreview, null);
});

test('a late measurement failure cannot clear the new tab measurement', async () => {
  const { store: s, api } = await fixture();
  await s.useDocument('a');
  const gate = deferred();
  api.measure = async id => {
    if (id === 'a') { await gate.promise; throw Error('old measurement failed'); }
    return { distance: 12 };
  };
  const old = s.measurePick({ face: 1 });
  await s.useDocument('b'); await s.measurePick({ face: 2 });
  gate.resolve(); await old;
  assert.equal(s.getState().measure.result.distance, 12);
  assert.equal(s.getState().error, null);
});


test('a feature preview is discarded when a dependency changes during its request', async () => {
  const { store: s, api, docs } = await fixture();
  await s.useDocument('a');
  const gate = deferred();
  api.previewMesh = async () => { await gate.promise; return { header: { ok: true, faces: 10 } }; };
  const old = s.previewFeature({ op: 'add_feature', kind: 'sketch', name: 's' });
  docs.a.revision += '+';
  await s.refetch();
  gate.resolve();
  assert.equal(await old, false);
  assert.equal(s.getState().ghostMesh, null);
});

test('switching tabs aborts a mesh request and ignores its late result', async () => {
  const { store: s, api } = await fixture();
  const gate = deferred(), mesh = api.mesh;
  let signal;
  api.mesh = async (id, options) => {
    if (id === 'a') { signal = options.signal; await gate.promise; }
    return mesh(id);
  };
  const old = s.useDocument('a');
  await settle(() => !!signal);
  await s.useDocument('b');
  assert.equal(signal.aborted, true);
  gate.resolve(); await old;
  assert.equal(s.getState().docId, 'b');
  assert.equal(s.getState().mesh.header.hash, 'b1');
});

test('rapid feature previews coalesce: one in flight, only the newest change waits', async () => {
  const { store: s, api } = await fixture();
  await s.useDocument('a');
  const gate = deferred(), sent = [];
  api.previewMesh = async (id, op) => { sent.push(op.name); if (sent.length === 1) await gate.promise; return { header: { ok: true, faces: 1 } }; };
  const first = s.previewFeature({ op: 'add_feature', kind: 'fillet', name: 'r1', args: {} });
  await tick();
  const second = s.previewFeature({ op: 'add_feature', kind: 'fillet', name: 'r2', args: {} });
  const third = s.previewFeature({ op: 'add_feature', kind: 'fillet', name: 'r3', args: {} });
  assert.deepEqual(sent, ['r1']);
  gate.resolve();
  assert.deepEqual(await Promise.all([first, second, third]), [true, false, true]);
  assert.deepEqual(sent, ['r1', 'r3']);
  assert.equal(s.getState().previewNote, 'preview: 1 faces');
});

test('clearing a preview drops the waiting change as well as the one in flight', async () => {
  const { store: s, api } = await fixture();
  await s.useDocument('a');
  const gate = deferred(), sent = [];
  api.previewMesh = async (id, op) => { sent.push(op.name); await gate.promise; return { header: { ok: true, faces: 1 } }; };
  const first = s.previewFeature({ op: 'add_feature', kind: 'fillet', name: 'r1', args: {} });
  await tick();
  const second = s.previewFeature({ op: 'add_feature', kind: 'fillet', name: 'r2', args: {} });
  s.clearFeaturePreview();
  gate.resolve();
  assert.deepEqual(await Promise.all([first, second]), [false, false]);
  assert.deepEqual(sent, ['r1']);
  assert.equal(s.getState().previewNote, null);
});

test('a document without a saved camera asks the viewport for a fit; a saved camera does not', async () => {
  const { store: s, api } = await fixture();
  await s.useDocument('a');
  assert.equal(s.getState().fitPending, true);
  s.fitApplied();
  assert.equal(s.getState().fitPending, false);
  api.getViews = async () => ({ camera: { position: [1, 2, 3] } });
  await s.useDocument('b');
  assert.equal(s.getState().fitPending, false);
  assert.deepEqual(s.getState().cameraToApply, { position: [1, 2, 3] });
});
