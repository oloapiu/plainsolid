// A dialog field filled by a viewport pick (a face, edge or vertex, as a selector expression)
// or by choosing a plane. Shared by the plane and feature dialogs.
import { useEffect, useState } from 'react';
import { useStore, requestPick, featureByName, hoverRefs, type PickTarget } from '../state/store';

export type Slot = PickTarget | null;

export function PickField({ label, value, kinds, planes, onPick, testId, autoArm }: {
  label: string; value: Slot; kinds: ('face' | 'edge' | 'vertex')[]; planes: boolean; onPick: (t: PickTarget) => void; testId: string;
  /** Arm the pick as soon as the field appears (when it is still empty). */
  autoArm?: boolean;
}) {
  const active = useStore((s) => s.pickRequest);
  const tree = useStore((s) => s.tree);
  const [arming, setArming] = useState(false);
  const isMine = arming && !!active;
  const arm = () => {
    if (isMine) { setArming(false); requestPick(null); return; }
    setArming(true);
    requestPick({ kinds, planes, hint: `pick ${label}: ${[...kinds.map((k) => `a ${k}`), ...(planes ? ['a plane'] : [])].join(' or ')}`,
                  onPick: (t) => { setArming(false); requestPick(null); onPick(t); } });
  };
  useEffect(() => { if (!active) setArming(false); }, [active]);
  useEffect(() => { if (autoArm && !value) arm(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const planeFeatures = (tree?.features ?? []).filter((f) => f.kind === 'plane' && f.variable);
  const choose = (name: string) => {
    if (!name) return;
    const standard = ['XY', 'XZ', 'YZ'].includes(name);
    const f = standard ? null : featureByName(name);
    onPick({ kind: 'plane', name, expr: standard ? name : (f?.variable ?? name), label: standard ? `${name} plane` : `plane ${name}`, standard });
  };
  return (
    <div className="pick-field" data-testid={testId}>
      <span className="pick-label">{label}</span>
      <span className={`pick-value ${value ? '' : 'empty'}`} title={value?.expr}
            onMouseEnter={() => hoverRefs(value && !(value.kind === 'plane' && value.standard) ? [value.expr] : null)} onMouseLeave={() => hoverRefs(null)}>{value ? value.label : 'nothing picked'}</span>
      <button className={`btn-small ${isMine ? 'active' : ''}`} onClick={arm} title={isMine ? 'stop picking' : 'click in the viewport'}>{isMine ? 'picking…' : 'pick'}</button>
      {planes && (
        <select className="pick-select" value="" onChange={(e) => choose(e.target.value)} title="a standard plane or a plane feature" data-testid={`${testId}-select`}>
          <option value="">plane…</option>
          {['XY', 'XZ', 'YZ'].map((n) => <option key={n} value={n}>{n}</option>)}
          {planeFeatures.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
        </select>
      )}
    </div>
  );
}
