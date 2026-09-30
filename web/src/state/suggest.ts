import { api } from '../api/client';
import type { EditOp, SuggestContext, Suggestion } from '../api/types';
import { set, state, withBusy } from './core';
import { edit } from './documents';
import { RequestLane } from './requests';
import { clearFeaturePreview, previewFeature, select, setStatus } from './tools';

// Suggestions: the hint box asks the configured model for one edit from what the user has
// selected and a few typed words; the proposal draws as a preview and waits for the person.
// Accepting is an ordinary edit (one undo step); nothing is written before that.

/** The open hint box: the document and selection it was opened on, what was asked, and the
 * answer. `pending` while the model thinks; `error` when no proposal passed the checks;
 * `setup` when no profile can be asked yet, and the box says how to configure one. */
export interface HintState {
  docId: string;
  setup: boolean;
  context: SuggestContext;
  asked: string | null;
  pending: boolean;
  proposal: Suggestion | null;
  error: string | null;
}

const lane = new RequestLane();

const PROFILE_KEY = 'plainsolid.suggestProfile';
const stored = () => { try { return globalThis.localStorage?.getItem(PROFILE_KEY) ?? null; } catch { return null; } };

/** Read the configured profiles and pick the one to ask: the one chosen here before, else the
 * configuration's default, else the first that is ready. */
export async function loadSuggestStatus() {
  let status;
  try { status = await api.suggestStatus(); } catch { status = { enabled: false, profiles: [] }; }
  const ready = status.profiles.filter((p) => p.ready).map((p) => p.name);
  const wanted = [state.suggestProfile, stored(), status.default ?? null];
  set({ suggest: status, suggestProfile: wanted.find((n) => n && ready.includes(n)) ?? ready[0] ?? null });
}

/** Ask this profile from now on (remembered in the browser); the new model's cache warms at once. */
export function setSuggestProfile(name: string) {
  if (!state.suggest?.profiles.some((p) => p.name === name && p.ready)) return;
  try { globalThis.localStorage?.setItem(PROFILE_KEY, name); } catch { /* private browsing: this session only */ }
  set({ suggestProfile: name });
  if (state.hint && !state.hint.setup) void api.suggestPrewarm(state.hint.docId, name).catch(() => {});
}

/** Open the hint box on the current document with the selection as the UI context, and warm
 * the model's cache while the hint is typed. With no profile to ask, the box opens to say how
 * to configure one. */
export function openHint(context: SuggestContext): boolean {
  if (!state.docId || !state.tree) return false;
  if (state.hint?.proposal) clearFeaturePreview();
  lane.cancel();
  const setup = !state.suggest?.enabled || !state.suggestProfile;
  set({ hint: { docId: state.docId, setup, context, asked: null, pending: false, proposal: null, error: null } });
  if (!setup) void api.suggestPrewarm(state.docId, state.suggestProfile).catch(() => { /* a cold cache only costs time */ });
  return true;
}

/** The setup view's "check again": read the configuration anew and, when a profile is ready,
 * turn the box into the hint box. */
export async function recheckSuggest() {
  await loadSuggestStatus();
  const h = state.hint;
  if (h?.setup && state.suggest?.enabled && state.suggestProfile) {
    set({ hint: { ...h, setup: false } });
    void api.suggestPrewarm(h.docId, state.suggestProfile).catch(() => {});
  }
}

/** Ask the model; a new ask replaces the one in flight. The proposal previews at once. */
export async function askHint(text: string) {
  const h = state.hint;
  const hint = text.trim();
  if (!h || h.setup || !hint) return;
  if (h.proposal) clearFeaturePreview();
  const request = lane.start();
  set({ hint: { ...h, asked: hint, pending: true, proposal: null, error: null } });
  try {
    const s = await withBusy(() => api.suggest(h.docId, hint, h.context, state.suggestProfile, request.signal));
    const now = state.hint;
    if (!request.current() || !now || now.docId !== h.docId || s.cancelled) return;
    if (!s.ok || !s.op) {
      set({ hint: { ...now, pending: false, error: s.error ?? 'no proposal' } });
      void report(s.id, 'failed', s.error ?? undefined);
      return;
    }
    set({ hint: { ...now, pending: false, proposal: s } });
    void previewFeature(s.op);
  } catch (e) {
    const now = state.hint;
    if (request.current() && now) set({ hint: { ...now, pending: false, error: (e as Error).message } });
  }
}

/** Write the proposal: an ordinary edit, so ctrl+z takes it back. `edited` records that the
 * person went on to change it (the code pane opens on it). Returns the feature it touched. */
export async function acceptHint(outcome: 'accepted' | 'edited' = 'accepted'): Promise<string | null> {
  const h = state.hint;
  const s = h?.proposal;
  if (!h || !s?.op) return null;
  if (h.docId !== state.docId || s.hash !== state.hash) {
    set({ hint: { ...h, proposal: null, error: 'the file changed since this was proposed: ask again' } });
    clearFeaturePreview();
    return null;
  }
  clearFeaturePreview();
  if (!(await edit(s.op))) {
    set({ hint: { ...h, error: state.error ?? 'the edit failed' } });
    return null;
  }
  void report(s.id, outcome);
  set({ hint: null });
  const name = touchedFeature(s.op);
  if (name && state.tree?.features.some((f) => f.name === name)) select(name);
  setStatus(`applied: ${s.label ?? 'suggestion'}`);
  return name;
}

/** Close the box; a shown proposal counts as dismissed. */
export function dismissHint() {
  const h = state.hint;
  if (!h) return;
  lane.cancel();
  if (h.proposal) {
    clearFeaturePreview();
    void report(h.proposal.id, 'dismissed');
  }
  set({ hint: null });
}

function report(id: string, outcome: 'accepted' | 'edited' | 'dismissed' | 'failed', detail?: string) {
  return api.suggestOutcome(id, outcome, detail).catch(() => { /* the journal is best effort */ });
}

/** The feature an operation adds or changes, to select after it is written. */
export function touchedFeature(op: EditOp): string | null {
  const o = op as { op: string; name?: string; feature?: string; sketch?: string; ops?: EditOp[] };
  if (o.op === 'add_feature') return o.name ?? null;
  if (o.op === 'set_argument') return o.feature ?? null;
  if (o.sketch) return o.sketch;
  if (o.op === 'batch') {
    for (const sub of [...(o.ops ?? [])].reverse()) {
      const name = touchedFeature(sub);
      if (name) return name;
    }
  }
  return null;
}
