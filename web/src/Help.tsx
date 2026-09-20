// The keyboard help: every shortcut on one card, opened with ? or the status bar's button.
export function Help({ onClose }: { onClose: () => void }) {
  const K = ({ k }: { k: string }) => <kbd>{k}</kbd>;
  const Row = ({ keys, what }: { keys: string[]; what: string }) => (
    <tr><td>{keys.map((k, i) => <span key={k}>{i > 0 ? ' ' : ''}<K k={k} /></span>)}</td><td>{what}</td></tr>
  );
  return (
    <div className="help-overlay" onClick={onClose} data-testid="help-overlay">
      <div className="help-card" onClick={(e) => e.stopPropagation()}>
        <h2>keys <span className="spacer" /><button className="btn-small" onClick={onClose}>close (esc)</button></h2>
        <div className="help-cols">
          <div>
            <h3>everywhere</h3>
            <table><tbody>
              <Row keys={['ctrl+z', 'ctrl+shift+z']} what="undo, redo (the server's stack; the code pane keeps its own while it has focus)" />
              <Row keys={['ctrl+o']} what="open a document" />
              <Row keys={['ctrl+tab']} what="next document tab" />
              <Row keys={['ctrl+`']} what="show or hide the code pane" />
              <Row keys={['delete']} what="delete the selected feature, instance, mate, view, dimension or note (asks when others go with it)" />
              <Row keys={['esc']} what="cancel a pick, a tool or a dialog, else clear the selection" />
              <Row keys={['?']} what="this card" />
            </tbody></table>
            <h3>viewport</h3>
            <table><tbody>
              <Row keys={['drag']} what="orbit · right drag or shift+middle drag pans · scroll zooms · the triad tips set a view" />
              <Row keys={['ctrl+1', '…', 'ctrl+7']} what="front, back, left, right, top, bottom, iso" />
              <Row keys={['f']} what="fit the model" />
              <Row keys={['ctrl+0']} what="look straight at the selected face (inside a sketch: at the sketch plane)" />
              <Row keys={['right-click']} what="actions for what is under the cursor, the selection and the active tool" />
              <Row keys={['shift+click']} what="add to a dialog's picks" />
            </tbody></table>
            <h3>assemblies</h3>
            <table><tbody>
              <Row keys={['m', 'r']} what="move tool: slide, turn" />
              <Row keys={['alt+click']} what="on a visibility checkbox: show only that instance" />
            </tbody></table>
          </div>
          <div>
            <h3>sketch</h3>
            <table><tbody>
              <Row keys={['l', 'c', 'a', 'r', 's', 'p', 'o']} what="line, circle, arc, rect, slot, polygon, point" />
              <Row keys={['d']} what="the dimension tool: click an entity or two, click empty space to place, type the value; stays on until esc or d" />
              <Row keys={['e']} what="convert body edges, vertices or a face outline" />
              <Row keys={['x']} what="the construction switch: new geometry, converts and offsets come out as construction while it is on" />
              <Row keys={['right-click']} what="relations, a dimension, offset, construction or delete for the selection; tools on empty space" />
              <Row keys={['shift+click']} what="add to the selection; on a body edge or vertex: relate to it" />
              <Row keys={['shift']} what="held while drawing: no snapping" />
              <Row keys={['alt+drag', 'middle drag']} what="orbit (a left drag on empty space orbits too)" />
              <Row keys={['ctrl+0']} what="normal to the sketch plane" />
              <Row keys={['enter']} what="close a polygon" />
              <Row keys={['delete']} what="delete the selected entities" />
              <Row keys={['esc']} what="stop the tool, then clear the selection (never leaves the sketch)" />
            </tbody></table>
            <h3>drawing</h3>
            <table><tbody>
              <Row keys={['drag']} what="move a view, dimension or note · drag the paper to pan · scroll zooms" />
              <Row keys={['esc']} what="stop the dimension or note tool" />
            </tbody></table>
            <h3>fields</h3>
            <table><tbody>
              <Row keys={['↑', '↓']} what="step a number by 1 · shift 10 · alt 0.1; the model follows" />
              <Row keys={['tab']} what="accept a name completion" />
            </tbody></table>
          </div>
        </div>
      </div>
    </div>
  );
}
