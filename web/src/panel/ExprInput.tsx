import { useEffect, useRef, useState } from 'react';

// A text field for numbers and expressions with autocomplete over parameter and
// dimension names. The suggestion list follows the identifier under the caret, so
// `wall * 2` and `holes.d1 + 1` both complete.
export function ExprInput({ text, names, onCommit, onCancel, disabled, autoFocus, className, placeholder, testId, commitUnchanged }: {
  text: string; names: string[]; onCommit: (t: string) => void; onCancel?: () => void; disabled?: boolean;
  autoFocus?: boolean; className?: string; placeholder?: string; testId?: string;
  /** Commit on Enter even when the text did not change (a prefilled value being accepted). */
  commitUnchanged?: boolean;
}) {
  const [val, setVal] = useState(text);
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const ref = useRef<HTMLInputElement>(null);
  const committed = useRef(false);
  // the blur commit runs on a timer: it must see the value and the prop as they are when it
  // fires, and a prop change (an undo, a refetch) cancels it, so a late timer never re-applies
  // what the user already typed once
  const valRef = useRef(text);
  const textRef = useRef(text);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // arrow keys step a plain number and commit after a short pause; while the steps are in flight an
  // older value coming back from the server must not reset the field
  const scrubTarget = useRef<string | null>(null);
  const scrubTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  valRef.current = val;
  textRef.current = text;
  useEffect(() => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    if (scrubTarget.current !== null) {
      if (text.trim() !== scrubTarget.current) return;
      scrubTarget.current = null;
    }
    setVal(text); committed.current = false;
  }, [text]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); if (scrubTimer.current) clearTimeout(scrubTimer.current); }, []);
  const step = (dir: number, e: React.KeyboardEvent<HTMLInputElement>): boolean => {
    const cur = valRef.current.trim();
    if (disabled || !/^-?\d+(\.\d+)?$/.test(cur)) return false;
    const delta = (e.shiftKey ? 10 : e.altKey ? 0.1 : 1) * dir;
    const decimals = Math.max((cur.split('.')[1] ?? '').length, e.altKey ? 1 : 0);
    const next = (Number(cur) + delta).toFixed(decimals);
    setVal(next); valRef.current = next; scrubTarget.current = next; committed.current = false;
    if (scrubTimer.current) clearTimeout(scrubTimer.current);
    scrubTimer.current = setTimeout(() => { scrubTimer.current = null; committed.current = true; onCommit(next); }, 250);
    return true;
  };
  useEffect(() => { if (autoFocus) { ref.current?.focus(); ref.current?.select(); } }, [autoFocus]);

  const token = () => {
    const el = ref.current;
    const caret = el?.selectionStart ?? val.length;
    const before = val.slice(0, caret);
    const m = /([A-Za-z_][A-Za-z0-9_.]*)$/.exec(before);
    return { word: m?.[1] ?? '', start: m ? caret - m[1].length : caret, caret };
  };
  const suggestions = (() => {
    if (!open) return [];
    const { word } = token();
    if (!word) return [];
    const w = word.toLowerCase();
    return names.filter((n) => n.toLowerCase().startsWith(w) && n !== word).slice(0, 8);
  })();
  const accept = (name: string) => {
    const { start, caret } = token();
    const next = val.slice(0, start) + name + val.slice(caret);
    setVal(next); setOpen(false);
    requestAnimationFrame(() => { const el = ref.current; if (el) { el.focus(); el.setSelectionRange(start + name.length, start + name.length); } });
  };
  const commit = (force = false) => {
    setOpen(false);
    if (committed.current) return;
    const current = valRef.current;
    if (force || current.trim() !== textRef.current.trim()) { committed.current = true; onCommit(current); }
  };
  return (
    <span className={`expr-input ${className ?? ''}`}>
      <input ref={ref} value={val} disabled={disabled} placeholder={placeholder} data-testid={testId} spellCheck={false}
        onChange={(e) => { setVal(e.target.value); setOpen(true); setIndex(0); }}
        onBlur={() => { if (timer.current) clearTimeout(timer.current); timer.current = setTimeout(() => { timer.current = null; setOpen(false); commit(); }, 120); }}
        onKeyDown={(e) => {
          if (suggestions.length && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { e.preventDefault(); setIndex((i) => (i + (e.key === 'ArrowDown' ? 1 : suggestions.length - 1)) % suggestions.length); return; }
          if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && step(e.key === 'ArrowUp' ? 1 : -1, e)) { e.preventDefault(); return; }
          if (suggestions.length && (e.key === 'Tab' || (e.key === 'Enter' && open))) { e.preventDefault(); accept(suggestions[index]); return; }
          if (e.key === 'Enter') { if (commitUnchanged) commit(true); (e.target as HTMLInputElement).blur(); }
          if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); setVal(text); onCancel?.(); (e.target as HTMLInputElement).blur(); }
        }} />
      {suggestions.length > 0 && (
        <ul className="expr-suggest">
          {suggestions.map((n, i) => (
            <li key={n} className={i === index ? 'active' : ''} onMouseDown={(e) => { e.preventDefault(); accept(n); }}>{n}</li>
          ))}
        </ul>
      )}
    </span>
  );
}
