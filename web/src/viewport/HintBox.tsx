import { useEffect, useRef, useState } from 'react';
import type { SuggestContext } from '../api/types';
import { goToCode } from '../menu/entries';
import { acceptHint, askHint, dismissHint, featureByName, getState, isAssembly, recheckSuggest, selectorTarget, setSuggestProfile, useStore } from '../state/store';
import { sceneRef } from './Viewport';

/** What the model is told the user is looking at: the mode, the sketch being edited, the
 * selection as the selectors a click writes, the feature selected in the tree. */
export function uiContext(): SuggestContext {
  const st = getState();
  if (st.sketchMode) return { mode: 'sketch', sketch: st.sketchMode.sketch, selection: [...st.sketchMode.selection] };
  const selection: SuggestContext['selection'] = [];
  const scene = sceneRef.current;
  for (const [kind, id] of [['face', st.selectedFace], ['edge', st.selectedEdge]] as const) {
    const c = id !== null && scene ? scene.entityCenter({ kind, id }) : null;
    const t = id !== null && c ? selectorTarget({ kind, id }, c) : null;
    if (t) selection.push({ kind: t.kind, expr: t.expr, label: t.label });
  }
  const ctx: SuggestContext = { mode: isAssembly(st.tree) ? 'assembly' : 'part', selection };
  if (st.selected) ctx.selected_feature = st.selected;
  if (st.selectedItem) ctx.selected_instance = st.selectedItem;
  return ctx;
}

function contextText(ctx: SuggestContext): string {
  const parts = ctx.selection.map((s) => (typeof s === 'string' ? s : s.label || s.expr));
  if (ctx.selected_feature && !parts.length) parts.push(ctx.selected_feature);
  if (ctx.selected_instance) parts.push(ctx.selected_instance);
  const where = ctx.mode === 'sketch' ? `sketch ${ctx.sketch}` : ctx.mode;
  return parts.length ? `${where} · ${parts.join(', ')}` : `${where} · nothing selected`;
}

/** What the setup view offers to copy: two profiles, one local and one hosted, keys by variable name. */
const EXAMPLE = `default = "local"

[profiles.local]                  # llama.cpp or vLLM on your network
base_url = "http://my-server:8000/v1"
model = "qwen3.6-35b-a3b-q8"
api = "llamacpp"
key_env = "LOCAL_MODEL_KEY"       # the variable holding the key, never the key
reasoning = "low"
reasoning_budget = 1000

[profiles.flash]                  # OpenRouter
base_url = "https://openrouter.ai/api/v1"
model = "qwen/qwen3.8-flash"
api = "openrouter"
key_env = "OPENROUTER_API_KEY"
reasoning = "low"
reasoning_budget = 1000
`;

/** No profile to ask: where the configuration goes, an example, and what is wrong with the
 * profiles there are. Keys stay in environment variables; nothing here takes one. */
function Setup() {
  const suggest = useStore((s) => s.suggest);
  const [copied, setCopied] = useState(false);
  const copy = async () => { try { await navigator.clipboard.writeText(EXAMPLE); setCopied(true); } catch { /* not permitted */ } };
  return (
    <div className="hint-setup" data-testid="hint-setup">
      <div className="hint-label">no model to ask yet</div>
      {suggest?.error && <div className="hint-error">{suggest.error}</div>}
      {(suggest?.profiles ?? []).filter((p) => !p.ready).map((p) => (
        <div key={p.name} className="hint-note">{p.name}: {p.problem}</div>
      ))}
      <div className="hint-info">
        Profiles live in <code>{suggest?.config ?? '~/.config/plainsolid/suggest.toml'}</code>. Each names the
        environment variable holding its key; set it where <code>plainsolid serve</code> runs, then check again.
      </div>
      <pre className="hint-diff">{EXAMPLE}</pre>
      <div className="hint-actions">
        <button className="btn-small active" type="button" onClick={() => void recheckSuggest()} data-testid="hint-recheck">check again</button>
        <button className="btn-small" type="button" onClick={() => void copy()}>{copied ? 'copied' : 'copy example'}</button>
        <button className="btn-small" type="button" onClick={dismissHint}>close esc</button>
      </div>
    </div>
  );
}

/** The profiles to choose from; one that cannot be asked shows why. */
function ProfilePicker() {
  const suggest = useStore((s) => s.suggest);
  const chosen = useStore((s) => s.suggestProfile);
  const profiles = suggest?.profiles ?? [];
  if (profiles.length < 2) return null;
  return (
    <select className="hint-profile" data-testid="hint-profile" value={chosen ?? ''} title="the model to ask"
            onChange={(e) => setSuggestProfile(e.target.value)}>
      {profiles.map((p) => (
        <option key={p.name} value={p.name} disabled={!p.ready} title={p.problem ?? `${p.model} · reasoning ${p.reasoning}`}>
          {p.name}{p.ready ? '' : ' (not ready)'}
        </option>
      ))}
    </select>
  );
}

/** The hint box: a few words in, one proposed edit out, previewed in the viewport. Enter
 * asks; on a proposal enter accepts, e accepts and opens it in the code pane, / refines,
 * esc dismisses. */
export function HintBox() {
  const hint = useStore((s) => s.hint);
  const docId = useStore((s) => s.docId);
  const [text, setText] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const proposal = hint?.proposal ?? null;
  const pending = hint?.pending ?? false;

  // a document switch closes the box; a closed box forgets its text, so the next one starts empty
  useEffect(() => { if (hint && docId !== hint.docId) dismissHint(); }, [docId, hint]);
  const open = hint !== null;
  useEffect(() => { if (!open) setText(''); }, [open]);
  useEffect(() => { if (hint && !hint.setup && !pending && !proposal) input.current?.focus(); }, [hint, pending, proposal]);
  useEffect(() => { if (proposal) card.current?.focus(); }, [proposal]);
  useEffect(() => {
    if (!pending) return;
    const t0 = Date.now();
    setElapsed(0);
    const id = window.setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 500);
    return () => window.clearInterval(id);
  }, [pending]);

  const acceptAndEdit = async () => {
    const name = await acceptHint('edited');
    const f = name ? featureByName(name) : null;
    if (f) goToCode(f);
  };

  // capture: the box owns these keys while it is open, before the app and the viewport see them
  useEffect(() => {
    if (!hint) return;
    const onKey = (e: KeyboardEvent) => {
      const inInput = e.target === input.current;
      const take = () => { e.preventDefault(); e.stopPropagation(); };
      if (e.key === 'Escape') { take(); dismissHint(); return; }
      if (inInput) return;
      if (!proposal || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'Enter') { take(); void acceptHint(); return; }
      if (e.key === 'e') { take(); void acceptAndEdit(); return; }
      if (e.key === '/') { take(); input.current?.focus(); input.current?.select(); }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [hint, proposal]);

  if (!hint) return null;
  if (hint.setup) return <div className="hint-box" data-testid="hint-box" ref={card} tabIndex={-1}><Setup /></div>;
  const diff = proposal?.diff?.split('\n').filter((l) => /^[+-](?![+-])/.test(l)).slice(0, 24) ?? [];
  return (
    <div className="hint-box" data-testid="hint-box" ref={card} tabIndex={-1}>
      <form className="hint-row" onSubmit={(e) => { e.preventDefault(); if (text.trim()) void askHint(text); }}>
        <input ref={input} className="hint-input" data-testid="hint-input" value={text} placeholder="what should change? e.g. 3mm fillet here"
               onChange={(e) => setText(e.target.value)} spellCheck={false} autoComplete="off" />
        <ProfilePicker />
      </form>
      <div className="hint-context" title="what the model is told you are looking at">{contextText(hint.context)}</div>
      {pending && (
        <div className="hint-status" data-testid="hint-pending">
          thinking… {elapsed}s <button className="btn-small" type="button" onClick={dismissHint}>cancel</button>
        </div>
      )}
      {hint.error && !pending && <div className="hint-error" data-testid="hint-error">{hint.error}</div>}
      {proposal && (
        <div className="hint-proposal" data-testid="hint-proposal">
          <div className="hint-label">{proposal.label}</div>
          {proposal.notes.map((n) => <div key={n} className="hint-note" data-testid="hint-note">{n}</div>)}
          {(proposal.info ?? []).map((n) => <div key={n} className="hint-info">{n}</div>)}
          {diff.length > 0 && (
            <pre className="hint-diff">{diff.map((l, i) => <div key={i} className={l.startsWith('+') ? 'add' : 'del'}>{l}</div>)}</pre>
          )}
          <div className="hint-actions">
            <button className="btn-small active" type="button" onClick={() => void acceptHint()} data-testid="hint-accept">accept ⏎</button>
            <button className="btn-small" type="button" onClick={() => void acceptAndEdit()} title="accept, then open it in the code pane">edit e</button>
            <button className="btn-small" type="button" onClick={dismissHint} data-testid="hint-dismiss">dismiss esc</button>
            <span className="hint-time" data-testid="hint-model">{proposal.model ? `${proposal.model} · ` : ''}{proposal.seconds.toFixed(1)}s{proposal.attempts.length > 1 ? ` · ${proposal.attempts.length} tries` : ''}</span>
          </div>
        </div>
      )}
    </div>
  );
}
