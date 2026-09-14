import { useEffect, useRef } from 'react';
import { EditorView, basicSetup } from 'codemirror';
import { python } from '@codemirror/lang-python';
import { EditorState, StateEffect, StateField, Compartment } from '@codemirror/state';
import { Decoration, type DecorationSet, keymap } from '@codemirror/view';
import { oneDark } from '@codemirror/theme-one-dark';
import { useStore, sourceEdited, flushSource, reloadSource, overwriteSource, getState, select } from '../state/store';

/** The editor, for scripts and for "go to code" (null until the pane mounts). */
export const codeViewRef: { current: EditorView | null } = { current: null };

/** Put the cursor at the start of a line and show it; the selection listener then selects the feature there. */
export function codeGoToLine(line: number, focus = true) {
  const v = codeViewRef.current;
  if (!v) return;
  const l = v.state.doc.line(Math.max(1, Math.min(line, v.state.doc.lines)));
  v.dispatch({ selection: { anchor: l.from }, effects: EditorView.scrollIntoView(l.from, { y: 'center' }) });
  if (focus) v.focus();
}

/** Where a name is defined: a parameter's line, or the first line of the feature bound to it. */
function definitionLine(name: string): number | null {
  const tree = getState().tree;
  if (!tree) return null;
  const p = tree.params.find((x) => x.name === name);
  if (p?.line) return p.line;
  const f = tree.features.find((x) => x.variable === name || x.name === name);
  return f?.span ? f.span[0] : null;
}

const setSpan = StateEffect.define<[number, number] | null>();
const setErrors = StateEffect.define<number[]>();
const spanMark = Decoration.line({ class: 'cm-feature-span' });
const errMark = Decoration.line({ class: 'cm-error-line' });

function lineDecorations(state: EditorState, span: [number, number] | null, errors: number[]): DecorationSet {
  const ranges: { from: number; deco: Decoration }[] = [];
  const n = state.doc.lines;
  if (span) for (let l = span[0]; l <= Math.min(span[1], n); l++) if (l >= 1) ranges.push({ from: state.doc.line(l).from, deco: spanMark });
  for (const l of errors) if (l >= 1 && l <= n) ranges.push({ from: state.doc.line(l).from, deco: errMark });
  ranges.sort((a, b) => a.from - b.from);
  return Decoration.set(ranges.map((r) => r.deco.range(r.from)), true);
}

const marks = StateField.define<{ span: [number, number] | null; errors: number[]; deco: DecorationSet }>({
  create: (state) => ({ span: null, errors: [], deco: lineDecorations(state, null, []) }),
  update(value, tr) {
    let { span, errors } = value;
    let changed = tr.docChanged;
    for (const e of tr.effects) {
      if (e.is(setSpan)) { span = e.value; changed = true; }
      if (e.is(setErrors)) { errors = e.value; changed = true; }
    }
    return changed ? { span, errors, deco: lineDecorations(tr.state, span, errors) } : value;
  },
  provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
});

/** The code pane: the file's text, its selected feature's span and error lines marked; the header
 * is the resize handle and holds the hide button; a closed pane keeps the header only. */
export function CodePane({ open, onToggle, onResize }: { open: boolean; onToggle: () => void; onResize: (height: number) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const source = useStore((s) => s.source);
  const dirty = useStore((s) => s.codeDirty);
  const saveState = useStore((s) => s.saveState);
  const externalPending = useStore((s) => s.externalPending);
  const closing = useStore((s) => s.closingDoc !== null && s.closingDoc === s.sourceDoc);
  const tree = useStore((s) => s.tree);
  const selected = useStore((s) => s.selected);
  const applying = useRef(false);
  const readOnly = useRef(new Compartment());

  useEffect(() => {
    if (!host.current) return;
    const v = new EditorView({
      state: EditorState.create({
        doc: getState().source,
        extensions: [
          basicSetup, python(), oneDark, marks, readOnly.current.of(EditorState.readOnly.of(false)),
          keymap.of([{ key: 'Mod-s', run: () => { flushSource(); return true; } }]),
          // ctrl+click (cmd+click) on a name jumps to where it is defined
          EditorView.domEventHandlers({
            mousedown: (e, v) => {
              if (!(e.ctrlKey || e.metaKey) || e.button !== 0) return false;
              const pos = v.posAtCoords({ x: e.clientX, y: e.clientY });
              if (pos === null) return false;
              const w = v.state.wordAt(pos);
              const line = w ? definitionLine(v.state.doc.sliceString(w.from, w.to)) : null;
              if (line === null) return false;
              e.preventDefault();
              codeGoToLine(line);
              return true;
            },
          }),
          EditorView.updateListener.of((u) => {
            if (u.docChanged && !applying.current) sourceEdited(u.state.doc.toString());
            if (u.selectionSet && !u.docChanged) {
              const line = u.state.doc.lineAt(u.state.selection.main.head).number;
              const f = getState().tree?.features.find((x) => x.span && line >= x.span[0] && line <= x.span[1]);
              if (f && f.name !== getState().selected) select(f.name);
            }
          }),
        ],
      }),
      parent: host.current,
    });
    view.current = v;
    codeViewRef.current = v;
    return () => { v.destroy(); codeViewRef.current = null; };
  }, []);

  useEffect(() => {
    view.current?.dispatch({ effects: readOnly.current.reconfigure(EditorState.readOnly.of(closing)) });
  }, [closing]);

  useEffect(() => {
    const v = view.current;
    if (!v) return;
    const current = v.state.doc.toString();
    if (current !== source && !getState().codeDirty) {
      applying.current = true;
      v.dispatch({ changes: { from: 0, to: current.length, insert: source } });
      applying.current = false;
    }
  }, [source, dirty]);

  useEffect(() => {
    const v = view.current;
    if (!v) return;
    const f = tree?.features.find((x) => x.name === selected);
    const errors = [
      ...(tree?.errors.map((e) => e.line).filter((l): l is number => l !== null) ?? []),
      ...(tree?.features.map((x) => x.result?.error?.line).filter((l): l is number => typeof l === 'number') ?? []),
    ];
    v.dispatch({ effects: [setSpan.of(f?.span ?? null), setErrors.of(errors)] });
    if (f?.span && !getState().codeDirty && document.activeElement !== v.contentDOM) {
      const line = v.state.doc.line(Math.min(f.span[0], v.state.doc.lines));
      v.dispatch({ effects: EditorView.scrollIntoView(line.from, { y: 'center' }) });
    }
  }, [tree, selected, open]);

  // the header drags the pane's height; a click on it without a drag does nothing
  const startResize = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!open || (e.target as HTMLElement).tagName === 'BUTTON') return;
    const el = e.currentTarget;
    const y0 = e.clientY, h0 = el.parentElement?.clientHeight ?? 220;
    el.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => onResize(h0 + (y0 - ev.clientY));
    const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
  };

  return (
    <div className={`code-pane ${open ? '' : 'closed'}`} data-testid="code-pane">
      <div className="code-header" onPointerDown={startResize} title={open ? 'drag to resize the code pane' : undefined}>
        <span>{tree?.path?.split('/').pop() ?? 'model'}</span>
        <span className={`code-state ${dirty ? 'dirty' : ''}`} role="status" data-testid="save-state">
          {{ saved: 'saved', unsaved: 'unsaved', saving: 'saving…', error: 'save failed', conflict: 'conflict' }[saveState]}
        </span>
        {saveState === 'error' && <button className="btn-small" onClick={() => void flushSource()} data-testid="save-retry">retry save</button>}
        {externalPending && (
          <span className="code-external">file changed on disk
            <button className="btn-small" onClick={reloadSource} title="discard this draft and load the file" data-testid="source-reload">reload file</button>
            <button className="btn-small" onClick={overwriteSource} title="replace the file with this draft" data-testid="source-overwrite">use my version</button>
          </span>
        )}
        <button className="btn-small code-toggle" onClick={onToggle} title={open ? 'hide the code pane (ctrl+`)' : 'show the code pane (ctrl+`)'} data-testid="code-toggle">{open ? 'hide code' : 'show code'}</button>
      </div>
      <div className="code-host" ref={host} hidden={!open} />
    </div>
  );
}
