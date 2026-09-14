// A small drop-down: a button with a caret and a list that closes on a choice or a click elsewhere.
import { useEffect, useRef, useState, type ReactNode } from 'react';

export function Menu({ label, title, testId, children, disabled }: { label: string; title?: string; testId?: string; children: ReactNode; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    window.addEventListener('pointerdown', onDown);
    window.addEventListener('keydown', onKey);
    return () => { window.removeEventListener('pointerdown', onDown); window.removeEventListener('keydown', onKey); };
  }, [open]);
  return (
    <span className="menu" ref={ref}>
      <button className={`btn-small ${open ? 'active' : ''}`} type="button" onClick={() => setOpen(!open)} title={title} data-testid={testId} disabled={disabled}>{label} ▾</button>
      {open && <div className="menu-pop" onClick={() => setOpen(false)}>{children}</div>}
    </span>
  );
}
