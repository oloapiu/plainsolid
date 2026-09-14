// The move tool for assemblies: drag an instance to slide or turn it as far as its mates allow.
import { useStore, setMoveMode } from '../state/store';

export function MovePanel() {
  const mode = useStore((s) => s.moveMode);
  const note = useStore((s) => s.previewNote);
  return (
    <div className="tool-panel" data-testid="move-panel">
      <span className="tool-title">move</span>
      <button className={`btn-small ${mode === 'translate' ? 'active' : ''}`} onClick={() => setMoveMode('translate')} title="slide the dragged part (m)" data-testid="move-translate">translate</button>
      <button className={`btn-small ${mode === 'rotate' ? 'active' : ''}`} onClick={() => setMoveMode('rotate')} title="turn the dragged part (r)" data-testid="move-rotate">rotate</button>
      <span className="sketch-hint">drag a part: it moves only as its mates allow, and the pose is written on release · esc</span>
      {note && <span className="status-error">{note}</span>}
    </div>
  );
}
