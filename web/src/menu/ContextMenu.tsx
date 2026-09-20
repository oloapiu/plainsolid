// The right-click menu: one component, entries computed by whoever opened it (a tree row, the
// viewport, the sketch overlay, the sheet). Closes on a choice, a click elsewhere, escape, a wheel.
import { useEffect, useRef } from 'react';
import { useStore, closeContextMenu } from '../state/store';

export function ContextMenu() {
  const menu = useStore((s) => s.contextMenu);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menu) return;
    const down = (e: PointerEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) closeContextMenu(); };
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); closeContextMenu(); } };
    const away = () => closeContextMenu();
    window.addEventListener('pointerdown', down, true);
    window.addEventListener('keydown', key, true);
    window.addEventListener('wheel', away, { passive: true });
    window.addEventListener('resize', away);
    return () => {
      window.removeEventListener('pointerdown', down, true);
      window.removeEventListener('keydown', key, true);
      window.removeEventListener('wheel', away);
      window.removeEventListener('resize', away);
    };
  }, [menu]);
  if (!menu) return null;
  const height = menu.entries.length * 24 + (menu.title ? 22 : 0) + 12;
  const x = Math.max(0, Math.min(menu.x, window.innerWidth - 230));
  const y = Math.max(0, Math.min(menu.y, window.innerHeight - Math.min(height, window.innerHeight - 8)));
  return (
    <div ref={ref} className="ctx-menu" style={{ left: x, top: y }} data-testid="context-menu" onContextMenu={(e) => e.preventDefault()}>
      {menu.title && <div className="ctx-title">{menu.title}</div>}
      {menu.entries.map((e, i) => (e.sep
        ? <div key={i} className="ctx-sep" />
        : <button key={i} className={`ctx-item ${e.danger ? 'danger' : ''}`} disabled={e.disabled} title={e.title} data-testid={`ctx-${e.id}`}
                  onClick={() => { closeContextMenu(); e.run?.(); }}>{e.label}{e.key && <span className="ctx-key">{e.key}</span>}</button>))}
    </div>
  );
}
