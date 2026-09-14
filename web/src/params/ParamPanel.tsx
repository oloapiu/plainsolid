import { useState } from 'react';
import { useStore, edit, expressionNames, unknownNames, setError } from '../state/store';
import type { Param } from '../api/types';
import { ExprInput } from '../panel/ExprInput';

/** The document's parameters with an add row; lives in the panel shown when nothing is selected. */
export function ParamPanel() {
  const tree = useStore((s) => s.tree);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  if (!tree) return null;
  const add = () => {
    const n = name.trim(), v = value.trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(n)) { setError('a parameter name is an identifier, such as wall_t'); return; }
    if (!v) { setError('give the parameter a value'); return; }
    const bad = unknownNames(v);
    if (bad.length) { setError(`unknown name${bad.length > 1 ? 's' : ''} in expression: ${bad.join(', ')}`); return; }
    void edit({ op: 'add_parameter', name: n, value: /^-?\d+(\.\d+)?$/.test(v) ? Number(v) : { expr: v } });
    setName(''); setValue('');
  };
  return (
    <div className="params" data-testid="param-list">
      {tree.params.length === 0 && <div className="panel-empty">no parameters yet: a top-level `name = number` line becomes one</div>}
      {tree.params.map((p) => <ParamRow key={p.name} p={p} />)}
      <form className="param-row param-add" onSubmit={(e) => { e.preventDefault(); add(); }} title="a new parameter goes after the last one, so every feature can use it">
        <input className="param-name" placeholder="name" value={name} onChange={(e) => setName(e.target.value)} data-testid="param-add-name" />
        <input className="param-value" placeholder="value or expression" value={value} onChange={(e) => setValue(e.target.value)} data-testid="param-add-value" />
        <button className="btn-small" type="submit" data-testid="param-add">+ parameter</button>
      </form>
    </div>
  );
}

function ParamRow({ p }: { p: Param }) {
  const text = p.expression ?? String(p.value);
  const commit = (t: string) => {
    t = t.trim();
    if (t === '' || t === text.trim()) return;
    const bad = unknownNames(t).filter((n) => n !== p.name);
    if (bad.length) { setError(`unknown name${bad.length > 1 ? 's' : ''} in expression: ${bad.join(', ')}`); return; }
    edit({ op: 'set_parameter', name: p.name, value: { expr: t } });
  };
  const isExpr = p.expression !== null && !/^-?\d+(\.\d+)?$/.test(p.expression.trim());
  return (
    <div className="param-row" title={p.line ? `line ${p.line} · ↑↓ step the value` : ''}>
      <span className="param-name">{p.name}</span>
      <ExprInput className="param-value" text={text} names={expressionNames().filter((n) => n !== p.name)} disabled={p.read_only} onCommit={commit} />
      {isExpr && <span className="param-computed">= {p.value}</span>}
      {p.description && <span className="param-desc">{p.description}</span>}
      {!p.read_only && <button className="doc-tab-close param-delete" title="delete the parameter (refused while a feature uses it)"
        onClick={() => void edit({ op: 'delete_parameter', name: p.name })} data-testid={`param-delete-${p.name}`}>×</button>}
    </div>
  );
}
