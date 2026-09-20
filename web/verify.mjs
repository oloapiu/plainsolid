// Drives the dev client against the API server. Usage: node verify.mjs [url] [projectDir]
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const URL = process.argv[2] || 'http://127.0.0.1:5173/';
const PROJ = process.argv[3] || process.env.PLAINSOLID_PROJ;
const FILE = path.join(PROJ, 'bracket.py');
const results = [];
const check = (name, ok, extra = '') => { results.push([name, ok]); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${extra}`); };
const readFile = () => fs.readFileSync(FILE, 'utf8');
/** Poll the file on disk until a predicate holds (the server writes after every edit), up to 15 s. */
const waitForFile = async (pred, ms = 15000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (pred(readFile())) return true; await new Promise((r) => setTimeout(r, 100)); } return pred(readFile()); };

const browser = await chromium.launch({ channel: 'chromium', headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
// a rejected edit is retried by the client, but it means two writers raced: record what was sent
page.on('response', (r) => { if (r.status() === 409) errors.push(`409 ${r.url().replace(/.*\/api/, '')} ${r.request().postData() ?? ''}`); });
page.on('dialog', (d) => d.accept('XZ'));

await page.goto(URL);
await page.waitForSelector('[data-testid=feature-profile]', { timeout: 20000 });
await page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('features'), null, { timeout: 20000 });
await page.waitForTimeout(800);
check('document loads with features', (await page.locator('.tree-row').count()) === 8);
await page.screenshot({ path: 'shot-initial.png' });

// click the viewport centre: selects a face and a feature
const vp = await page.locator('[data-testid=viewport] canvas').boundingBox();
let hit = null;
for (const [fx, fy] of [[0.5, 0.62], [0.5, 0.7], [0.4, 0.5], [0.6, 0.6], [0.5, 0.5]]) {
  await page.mouse.move(vp.x + vp.width * fx, vp.y + vp.height * fy);
  await page.waitForTimeout(250);
  if (await page.locator('.viewport-hover').count()) { hit = [fx, fy]; break; }
}
check('hovering the body shows a face label', hit !== null, hit ? `at ${hit}` : '');
await page.mouse.down(); await page.mouse.up();
await page.waitForTimeout(400);
const selectedRow = await page.locator('.tree-row.selected').count();
const propsTitle = await page.locator('.props-title').textContent();
check('click on the body selects a face and its feature', selectedRow === 1, `props: ${propsTitle?.trim()}`);

// select the extrude in the tree and change its depth
await page.click('[data-testid=feature-body]');
await page.waitForTimeout(200);
const depth = page.locator('.field input').first();
check('extrude property panel shows depth expression', (await depth.inputValue()) === 'base_depth');
const hashBefore = await page.locator('.status-hash').textContent();
await depth.fill('45'); await depth.press('Enter');
await page.waitForFunction((h) => document.querySelector('.status-hash')?.textContent !== h, hashBefore, { timeout: 15000 });
await page.waitForTimeout(600);
check('set_argument writes the file', readFile().includes('extrude("body", profile, 45)'));

// undo with ctrl+z restores the file
await page.locator('[data-testid=viewport] canvas').focus();
await page.keyboard.press(process.platform === 'darwin' ? 'Meta+z' : 'Control+z');
await waitForFile((t) => t.includes('extrude("body", profile, base_depth)'));
await page.waitForTimeout(600);
check('undo restores the file', readFile().includes('extrude("body", profile, base_depth)'));

// parameters tab
await page.evaluate(() => window.__plainsolid.actions.select(null));  // the parameters live in the panel shown when nothing is selected
await page.waitForSelector('[data-testid=param-list]');
const thick = page.locator('.param-row', { hasText: 'thickness' }).locator('input');
await thick.fill('5'); await thick.press('Enter');
await page.waitForFunction(() => window.__plainsolid.getState().tree?.params?.[0]?.value === 5, null, { timeout: 15000 });
await page.waitForTimeout(400);
check('parameter edit writes the file (a whole number over 4.0 stays a float)', /^thickness = 5\.0\s/m.test(readFile()));
await thick.fill('4.0'); await thick.press('Enter');
await page.waitForFunction(() => window.__plainsolid.getState().tree?.params?.[0]?.value === 4, null, { timeout: 15000 });
await page.waitForTimeout(400);

// rollback
await page.locator('[data-testid=feature-body] .tree-rollback').click();
await page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('rolled back'), null, { timeout: 15000 });
check('rollback refetches the mesh up to a feature', (await page.locator('.tree-row.rolled-back').count()) === 6);
await page.click('.tree-showall');
await page.waitForTimeout(800);

// the extrude button on a sketch opens a dialog that previews the result; add writes the feature after the sketch
await page.click('[data-testid=feature-holes]');
await page.click('[data-testid=add-extrude]');
await page.waitForSelector('[data-testid=feature-dialog]');
await page.waitForFunction(() => window.__plainsolid.hasGhost() && window.__plainsolid.getState().ghostStyle === 'preview', null, { timeout: 15000 }).catch(() => {});
const extrudePreview = await page.evaluate(() => window.__plainsolid.hasGhost() && window.__plainsolid.getState().ghostStyle === 'preview');
const previewNote = await page.locator('[data-testid=feature-preview]').textContent().catch(() => '');
await page.click('[data-testid=feature-ok]');
await page.waitForSelector('[data-testid=feature-extrude1]', { timeout: 15000 });
await page.waitForTimeout(500);
check('the extrude dialog previews the result and adds the feature after the sketch', extrudePreview && readFile().includes('extrude1 = extrude("extrude1", holes, 10)') && !(await page.evaluate(() => window.__plainsolid.hasGhost())), `preview: ${previewNote}`);
await page.click('.btn-small.danger:has-text("delete")');
await waitForFile((t) => !t.includes('extrude1'));
await page.waitForTimeout(400);
check('delete removes the feature', !readFile().includes('extrude1'));

// new sketch from the plane menu (nothing selected): XZ
await page.click('[data-testid=new-sketch]');
await page.click('[data-testid=sketch-on-XZ]');
await page.waitForSelector('[data-testid=feature-sketch1]', { timeout: 15000 });
check('new sketch added on XZ', readFile().includes('sketch1 = sketch("sketch1", on=XZ)'));
await page.waitForSelector('[data-testid=sketch-bar]', { timeout: 10000 }).catch(() => {});
await page.locator('[data-testid=sketch-exit]').click().catch(() => {}); await page.waitForTimeout(400);

// sketch mode: draw a circle with two clicks
await page.click('[data-testid=feature-holes]');
await page.click('.btn:has-text("edit sketch")');
await page.waitForSelector('[data-testid=sketch-bar]');
await page.click('.sketch-bar .btn-small:has-text("circle")');
await page.waitForTimeout(300);
await page.mouse.move(vp.x + vp.width * 0.5, vp.y + vp.height * 0.5); await page.waitForTimeout(100);
await page.mouse.down(); await page.mouse.up();
await page.mouse.move(vp.x + vp.width * 0.55, vp.y + vp.height * 0.5); await page.waitForTimeout(100);
await page.mouse.down(); await page.mouse.up();
await page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('added circle'), null, { timeout: 15000 });
await page.waitForTimeout(400);
check('sketch mode adds a circle with literal coordinates', /holes\.circle\("circle1", [\d.]+, at=\(-?\d+, -?\d+\)\)/.test(readFile()), readFile().match(/holes\.circle\("circle1".*/)?.[0] ?? '');
await page.screenshot({ path: 'shot-sketch.png' });
// drag the circle by its curve: the diameter follows the cursor, the centre stays
{
  const circ = readFile().match(/holes\.circle\("circle1", ([\d.]+), at=\((-?\d+), (-?\d+)\)\)/);
  const d0 = Number(circ[1]), cx = Number(circ[2]), cy = Number(circ[3]);
  // the top of the circle: its right side sits under a dimension label of the sketch, which takes the press
  const rim = await page.evaluate(([u, v]) => window.__plainsolid.sketchToScreen(u, v), [cx, cy + d0 / 2]);
  const far = await page.evaluate(([u, v]) => window.__plainsolid.sketchToScreen(u, v), [cx, cy + d0 / 2 + 4]);
  await page.keyboard.press('Escape');  // leave the circle tool
  await page.waitForTimeout(200);
  const hashRim = await page.locator('.status-hash').textContent();
  await page.mouse.move(rim[0], rim[1]); await page.waitForTimeout(150);
  await page.mouse.down(); await page.mouse.move(far[0], far[1], { steps: 6 }); await page.waitForTimeout(400); await page.mouse.up();
  await page.waitForFunction((h) => document.querySelector('.status-hash')?.textContent !== h, hashRim, { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(500);
  const after = readFile().match(/holes\.circle\("circle1", ([\d.]+), at=\((-?\d+), (-?\d+)\)\)/);
  check('dragging a circle by its curve grows its diameter and leaves its centre alone',
    !!after && Number(after[1]) > d0 + 5 && Number(after[1]) < d0 + 11 && Number(after[2]) === cx && Number(after[3]) === cy,
    `${circ?.[0]} -> ${after?.[0]}`);
}
// draw a rectangle
await page.click('.sketch-bar .btn-small:has-text("rect")');
await page.mouse.move(vp.x + vp.width * 0.3, vp.y + vp.height * 0.3); await page.mouse.down(); await page.mouse.up();
await page.mouse.move(vp.x + vp.width * 0.4, vp.y + vp.height * 0.4); await page.mouse.down(); await page.mouse.up();
await page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('added rect'), null, { timeout: 15000 });
await page.waitForTimeout(400);
check('sketch mode adds a rect', /holes\.rect\("rect1", \d+, \d+, at=\(/.test(readFile()));
await page.keyboard.press('Escape'); await page.keyboard.press('Escape'); await page.keyboard.press('Escape');
await page.waitForTimeout(300);
const stillSketching = (await page.locator('[data-testid=sketch-bar]').count()) === 1;
await page.click('[data-testid=sketch-exit]');
await page.waitForTimeout(300);
check('escape never leaves sketch mode; the exit button does', stillSketching && (await page.locator('[data-testid=sketch-bar]').count()) === 0);

// external edit shows up in the code pane through the watcher
fs.appendFileSync(FILE, '\n# external edit\n');
await page.waitForFunction(() => document.querySelector('.cm-content')?.textContent?.includes('# external edit'), null, { timeout: 15000 }).then(() => check('external edit reaches the code pane', true)).catch(() => check('external edit reaches the code pane', false));

// snapshot downloads a png
await page.click('[data-testid=file-menu]');
const [download] = await Promise.all([page.waitForEvent('download', { timeout: 15000 }), page.click('[data-testid=snapshot]')]);
const dl = await download.path();
check('snapshot downloads a PNG', dl !== null && fs.statSync(dl).size > 1000);

// ---- the compare overlay -----------------------------------------------------------------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  await page.evaluate(() => window.__plainsolid.actions.select(null));
  await page.click('[data-testid=file-menu]');
  await page.click('[data-testid=compare-file]');
  await page.click('[data-testid="file-lid.py"]');
  await page.waitForFunction(() => !!window.__plainsolid.getState().mesh?.header.compare, null, { timeout: 30000 }).catch(() => {});
  const cs = await st();
  const items = (cs.mesh?.header.items ?? []).map((i) => i.name);
  const reportText = await page.locator('[data-testid=compare-report]').textContent().catch(() => '');
  check('comparing with another file overlays common, added and removed and the panel reports the volumes',
    items.join(',') === 'common,added,removed' && cs.overlay?.other === 'lid.py' && /added/.test(reportText) && cs.status.includes('comparing with lid.py'),
    `${items.join(',')} · ${cs.status}`);
  await page.click('[data-testid=compare-stop-panel]');
  await page.waitForFunction(() => !window.__plainsolid.getState().overlay && !window.__plainsolid.getState().mesh?.header.compare, null, { timeout: 15000 }).catch(() => {});
  const back = await st();
  check('stop comparing restores the plain mesh', !back.overlay && !back.mesh?.header.compare && (back.mesh?.header.items ?? []).length <= 1);
  // the scratch project is not a git repository: comparing with the last commit says so and stays on the plain mesh
  const errorsBefore = errors.length;
  await page.click('[data-testid=file-menu]');
  await page.click('[data-testid=compare-head]');
  await page.waitForFunction(() => /git/.test(window.__plainsolid.getState().error ?? ''), null, { timeout: 15000 }).catch(() => {});
  const noGit = await st();
  check('comparing with the last commit outside git reports the reason', /git/.test(noGit.error ?? '') && !noGit.overlay, noGit.error ?? '');
  errors.splice(errorsBefore);  // the 400 the browser logged is that answer
  await page.evaluate(() => window.__plainsolid.actions.setError(null));
}

// ---- the parameters tab: add and delete a parameter -------------------------------------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  await page.evaluate(() => window.__plainsolid.actions.select(null));  // the parameters live in the panel shown when nothing is selected
await page.waitForSelector('[data-testid=param-list]');
  await page.fill('[data-testid=param-add-name]', 'lip');
  await page.fill('[data-testid=param-add-value]', 'thickness / 2');
  let h = (await st()).hash;
  await page.click('[data-testid=param-add]');
  await page.waitForFunction((h) => window.__plainsolid.getState().hash !== h, h, { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(500);
  const withLip = fs.readFileSync(FILE, 'utf8');
  const lipLine = withLip.split('\n').findIndex((l) => l.startsWith('lip = thickness / 2'));
  const lastParam = withLip.split('\n').findIndex((l) => l.startsWith('slot_spacing = '));
  check('the parameters tab adds a parameter right after the last one', lipLine === lastParam + 1, `lip at line ${lipLine}, slot_spacing at ${lastParam}`);
  h = (await st()).hash;
  await page.click('[data-testid=param-delete-lip]');
  await page.waitForFunction((h) => window.__plainsolid.getState().hash !== h, h, { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(500);
  check('the delete cross removes an unused parameter', !fs.readFileSync(FILE, 'utf8').includes('lip = '));
  const errorsBeforeRefusal = errors.length;
  const refused = await page.evaluate(() => window.__plainsolid.actions.edit({ op: 'delete_parameter', name: 'width' }));
  const used = await st();
  check('a parameter in use refuses to be deleted and says who uses it', !refused && /still used/.test(used.error ?? '') && fs.readFileSync(FILE, 'utf8').includes('width = 60'), used.error ?? '');
  check('the error shows in a banner over the viewport', (await page.locator('[data-testid=error-banner]').count()) === 1);
  errors.splice(errorsBeforeRefusal);  // the 400 the browser logged is that refusal
  await page.evaluate(() => window.__plainsolid.actions.setError(null));
  }

// ---- keys, the code pane, the help card, tree summaries, scrubbing, the code cursor ------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  await page.click('[data-testid=feature-inner]');
  await page.waitForTimeout(150);
  await page.locator('[data-testid=viewport] canvas').focus();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(150);
  check('escape clears the selection', (await st()).selected === null && (await page.locator('[data-testid=part-props]').count()) === 1);
  check('the part panel shows the size line and the parameters', /g · .* cm³ · 60 × 40 × 50 mm · \d+ faces/.test(await page.locator('[data-testid=part-summary]').textContent().catch(() => '')) && (await page.locator('[data-testid=param-list] .param-row').count()) >= 6,
    await page.locator('[data-testid=part-summary]').textContent().catch(() => ''));
  check('tree rows carry the defining value', (await page.locator('[data-testid=feature-inner] .tree-kind').textContent()) === 'R3' && (await page.locator('[data-testid=feature-body] .tree-kind').textContent()) === 'profile · 40'
    && (await page.locator('[data-testid=feature-hole_cut] .tree-kind').textContent()) === 'holes · through');
  // the delete key asks first when the feature has dependants
  await page.click('[data-testid=feature-profile]');
  await page.locator('[data-testid=viewport] canvas').focus();
  await page.keyboard.press('Delete');
  await page.waitForTimeout(200);
  const asked = (await page.locator('[data-testid=delete-confirm]').count()) === 1;
  await page.keyboard.press('Escape');
  await page.waitForTimeout(150);
  check('the delete key opens the cascade confirmation and escape closes it', asked && (await page.locator('[data-testid=delete-confirm]').count()) === 0 && readFile().includes('sketch("profile"'));
  // hovering a tree row lights the feature in the viewport
  await page.hover('[data-testid=feature-inner]');
  await page.waitForTimeout(100);
  const hoverOn = (await st()).treeHover?.feature === 'inner';
  await page.mouse.move(vp.x + 5, vp.y + 5);
  await page.waitForTimeout(100);
  check('hovering a tree row lights its feature', hoverOn && (await st()).treeHover === null);
  // the code pane hides and shows, the help card opens with ? and closes with escape
  await page.click('[data-testid=code-toggle]');
  await page.waitForTimeout(150);
  const hidden = await page.locator('.code-host').isHidden();
  await page.click('[data-testid=code-toggle]');
  await page.waitForTimeout(150);
  check('the code pane hides and shows from its header button', hidden && await page.locator('.code-host').isVisible());
  await page.locator('[data-testid=viewport] canvas').focus();
  await page.keyboard.press('?');
  await page.waitForTimeout(150);
  const helpOpen = (await page.locator('[data-testid=help-overlay]').count()) === 1;
  await page.keyboard.press('Escape');
  await page.waitForTimeout(150);
  check('? opens the keys card and escape closes it', helpOpen && (await page.locator('[data-testid=help-overlay]').count()) === 0);
  // the code cursor selects the feature on its line
  await page.evaluate(() => window.__plainsolid.codeCursorTo(40));
  await page.waitForTimeout(200);
  check('the code cursor on a line selects its feature', (await st()).selected === 'inner');
  // hovering a reference in the panel lights what it picks
  await page.click('[data-testid=feature-inner]');
  await page.waitForSelector('[data-testid=ref-line]');
  await page.hover('[data-testid=ref-line] .refs');
  await page.waitForFunction(() => window.__plainsolid.getState().refHighlight.length > 0, null, { timeout: 15000 }).catch(() => {});
  const litRefs = (await st()).refHighlight.length;
  await page.mouse.move(vp.x + 5, vp.y + 5);
  await page.waitForTimeout(200);
  check('hovering the fillet\'s edges reference lights those edges', litRefs >= 1 && (await st()).refHighlight.length === 0, `${litRefs} entities`);
  await page.evaluate(() => window.__plainsolid.actions.select(null));
  // arrow keys step a parameter and the model follows
  await page.waitForSelector('[data-testid=param-list]');
  const thickField = page.locator('.param-row', { hasText: 'thickness' }).locator('input');
  await thickField.focus();
  await thickField.press('ArrowUp');
  await page.waitForFunction(() => window.__plainsolid.getState().tree?.params?.[0]?.value === 5, null, { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(300);
  const stepped = /^thickness = 5\.0\s/m.test(readFile());
  await thickField.press('ArrowDown');
  await page.waitForFunction(() => window.__plainsolid.getState().tree?.params?.[0]?.value === 4, null, { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(300);
  check('arrow keys step a parameter and the file follows', stepped && /^thickness = 4\.0\s/m.test(readFile()));
  await page.locator('[data-testid=viewport] canvas').focus();
}

// ---- right-click menus and the open popover ----------------------------------------------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  const menuLabels = async () => (await st()).contextMenu?.entries.filter((e) => !e.sep).map((e) => e.label) ?? [];
  await page.click('[data-testid=feature-inner]', { button: 'right' });
  await page.waitForSelector('[data-testid=context-menu]');
  const rowMenu = await menuLabels();
  check('a right-click on a tree row selects it and offers suppress, roll back, code and delete', (await st()).selected === 'inner' && ['suppress', 'roll back to here', 'go to code', 'delete'].every((l) => rowMenu.includes(l)), rowMenu.join(' | '));
  await page.click('[data-testid=ctx-suppress]');
  await waitForFile((t) => /fillet\("inner",.*suppressed=True/.test(t));
  await page.waitForTimeout(400);
  const suppressed = /fillet\("inner",.*suppressed=True/.test(readFile());
  await page.click('[data-testid=feature-inner]', { button: 'right' });
  await page.waitForSelector('[data-testid=ctx-unsuppress]');
  await page.click('[data-testid=ctx-unsuppress]');
  await waitForFile((t) => !/suppressed=True/.test(t));
  await page.waitForTimeout(400);
  check('the menu suppresses and unsuppresses the feature', suppressed && !/suppressed=True/.test(readFile()));
  await page.click('[data-testid=feature-holes]', { button: 'right' });
  await page.waitForSelector('[data-testid=context-menu]');
  const sketchRow = await menuLabels();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(100);
  check('a sketch row offers edit, extrude, cut and revolve; escape closes the menu', ['edit sketch', 'extrude…', 'cut…', 'revolve…'].every((l) => sketchRow.includes(l)) && (await page.locator('[data-testid=context-menu]').count()) === 0, sketchRow.join(' | '));
  // a face in the viewport
  if (hit) {
    await page.mouse.click(vp.x + vp.width * hit[0], vp.y + vp.height * hit[1], { button: 'right' });
    await page.waitForSelector('[data-testid=context-menu]');
    const faceMenu = await menuLabels();
    const title = (await st()).contextMenu?.title ?? '';
    await page.keyboard.press('Escape');
    const expected = title.startsWith('edge') ? ['fillet…', 'chamfer…', 'plane at an angle about it…'] : ['sketch on this face', 'offset plane from it…', 'normal to'];
    check('a right-click on body geometry offers its actions, measure and the views', [...expected, 'measure from here', 'fit'].every((l) => faceMenu.includes(l)) && /^(face|edge) \d+ of /.test(title), `${title}: ${faceMenu.join(' | ')}`);
  }
  await page.evaluate(() => window.__plainsolid.actions.select(null));
  // the open popover lists the project's documents by kind
  await page.click('[data-testid=open-menu]');
  await page.waitForSelector('[data-testid=files-pop]');
  const listed = await page.locator('[data-testid=files-pop] .files-row').count();
  await page.fill('[data-testid=open-path]', 'lid');
  const filtered = await page.locator('[data-testid=files-pop] .files-row').count();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(100);
  check('the open popover lists the documents and filters them', listed >= 5 && filtered === 1 && (await page.locator('[data-testid=files-pop]').count()) === 0, `${listed} listed, ${filtered} match "lid"`);
}

// ---- sketching -----------------------------------------------------------------------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  const profileSol = async () => (await st()).tree.features.find((f) => f.name === 'profile').result.sketch;
  const at = (u, v) => page.evaluate(([u, v]) => window.__plainsolid.sketchToScreen(u, v), [u, v]);
  const clickAt = async (u, v, mods = []) => {
    const p = await at(u, v); await page.mouse.move(p[0], p[1]); await page.waitForTimeout(150);
    for (const m of mods) await page.keyboard.down(m);
    await page.mouse.down(); await page.mouse.up();
    for (const m of mods) await page.keyboard.up(m);
    await page.waitForTimeout(250);
  };
  const waitHash = async (h) => page.waitForFunction((h) => { const s = window.__plainsolid.getState(); return s.hash !== h && s.tree?.hash === s.hash; }, h, { timeout: 15000 }).then(() => page.waitForTimeout(700));
  const changedLines = (a, b) => { const x = a.split('\n'), y = b.split('\n'); const out = []; for (let i = 0; i < Math.max(x.length, y.length); i++) if (x[i] !== y[i]) out.push(y[i] ?? x[i]); return out; };

  await page.click('[data-testid=feature-profile]');
  await page.click('.btn:has-text("edit sketch")');
  await page.waitForSelector('[data-testid=sketch-bar]');
  await page.waitForTimeout(1200);
  const sol0 = await profileSol();
  check('sketch renders from solved coordinates and reports fully constrained',
    sol0.fully_constrained && (await page.locator('[data-testid=sketch-dof]').textContent()) === 'fully constrained' && (await page.locator('.dim-label').count()) === 3,
    `dof ${sol0.dof}, labels ${await page.locator('.dim-label').count()}, glyphs ${await page.locator('.glyph').count()}`);

  // delete the height dimension: one degree of freedom, the wall entities go free (blue)
  let h = (await st()).hash;
  await page.locator('[data-testid=constraint-h] .btn-small.danger').click();
  await waitHash(h);
  const sol1 = await profileSol();
  check('deleting a dimension shows one degree of freedom and frees the wall entities',
    sol1.dof === 1 && sol1.free_entities.includes('outer_wall') && (await page.locator('[data-testid=sketch-dof]').textContent()) === '1 degree of freedom',
    `free ${sol1.free_entities}`);

  // drag the wall top: only the three wall lines change in the file
  const beforeDrag = readFile();
  let p = await at(-26, 50); await page.mouse.move(p[0], p[1]); await page.waitForTimeout(150); await page.mouse.down();
  const q = await at(-26, 62);
  for (let i = 1; i <= 8; i++) { await page.mouse.move(p[0] + (q[0] - p[0]) * i / 8, p[1] + (q[1] - p[1]) * i / 8); await page.waitForTimeout(50); }
  await page.waitForFunction(() => window.__plainsolid.getState().sketchMode?.preview !== null, null, { timeout: 10000 }).catch(() => {});
  const previewTop = (await st()).sketchMode.preview?.top?.start ?? null;
  h = (await st()).hash;
  await page.mouse.up(); await waitHash(h);
  const dragLines = changedLines(beforeDrag, readFile());
  check('dragging a free point previews the solve and writes back only the wall coordinates',
    previewTop !== null && dragLines.length === 3 && dragLines.every((l) => /inner_wall|"top"|outer_wall/.test(l)) && /profile\.line\("top", \(-26, 6[12](?:\.\d+)?\), \(-30, 6[12](?:\.\d+)?\)\)/.test(readFile()),
    `preview ${JSON.stringify(previewTop)}, changed ${dragLines.length}: ${dragLines.join(' | ')}`);

  // a new line with horizontal inference: one batch, two statements
  await page.click('.sketch-bar .btn-small:has-text("line")');
  await clickAt(40, 20); h = (await st()).hash; await clickAt(60, 20.3); await waitHash(h);
  await page.keyboard.press('Escape'); await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  check('drawing a near-horizontal line writes the line and its inferred horizontal constraint in one commit',
    /profile\.line\("line1", \(40, 20\), \(60, 20\)\)/.test(readFile()) && /profile\.horizontal\("h\d+", "line1"\)/.test(readFile()),
    readFile().match(/profile\.(line|horizontal)\("(line1|h\d+)".*/g)?.join(' | ') ?? '');

  // select two lines: the panel's selected section offers parallel, perpendicular, equal; apply parallel (implied by H + H: redundant)
  await clickAt(10, 0); await clickAt(0, 4, ['Shift']);
  await page.waitForSelector('[data-testid=sketch-selected]', { timeout: 5000 });
  const bar = (await page.locator('[data-testid=sketch-selected]').textContent().catch(() => '')) ?? '';
  // the right-click menu on a selected line carries the same relations, with the keys on the right
  let pr = await at(10, 0); await page.mouse.click(pr[0], pr[1], { button: 'right' });
  await page.waitForSelector('[data-testid=context-menu]', { timeout: 5000 });
  const menuText = (await page.locator('[data-testid=context-menu]').textContent()) ?? '';
  const menuHasKeys = (await page.locator('[data-testid=context-menu] .ctx-key').count()) >= 2;
  await page.keyboard.press('Escape'); await page.waitForTimeout(150);
  h = (await st()).hash;
  await page.click('[data-testid=constrain-parallel]'); await waitHash(h);
  const sol2 = await profileSol();
  check('selecting two lines offers line constraints in the panel and the menu, and applying parallel writes it and flags it redundant',
    /parallel/.test(bar) && /perpendicular/.test(bar) && /equal/.test(bar) && /parallel/.test(menuText) && /offset…/.test(menuText) && /delete/.test(menuText) && menuHasKeys
      && /profile\.parallel\("pa1", "bottom", "inner_bottom"\)/.test(readFile()) && sol2.redundant.includes('pa1'),
    `panel: ${bar.slice(0, 80)}, menu: ${menuText.slice(0, 120)}, keys ${menuHasKeys}, redundant ${sol2.redundant}`);

  // the dimension tool on the new line: the pick, a preview that follows the cursor, the placement click, the value box;
  // the length is written with where its label went, and the tool stays on
  await clickAt(50, 20); await page.keyboard.press('d'); await page.waitForTimeout(300);
  let pm = await at(50, 27); await page.mouse.move(pm[0], pm[1]); await page.waitForTimeout(200);
  const previewText = (await page.locator('[data-testid=snap-glyph]').textContent().catch(() => '')) ?? '';
  await clickAt(50, 27);
  await page.waitForSelector('[data-testid=dim-pending-input]', { timeout: 5000 });
  const prefill = await page.locator('[data-testid=dim-pending-input]').inputValue();
  h = (await st()).hash;
  await page.locator('[data-testid=dim-pending-input]').press('Enter'); await waitHash(h);
  const toolStays = (await st()).sketchMode?.tool === 'dimension';
  check('the dimension tool previews the length under the cursor, writes it with its label place, and stays on',
    previewText === '20' && prefill === '20' && toolStays && /profile\.length\("len1", "line1", 20, at=\(50, 27\)\)/.test(readFile()) && (await page.locator('[data-testid=dim-len1]').textContent())?.startsWith('20'),
    `preview ${previewText}, prefill ${prefill}, tool ${toolStays}, ${readFile().match(/profile\.length\("len1".*/)?.[0] ?? 'no statement'}`);

  // two points: where the cursor places the label decides what is measured (right of the pair: the vertical distance)
  await clickAt(-30, 0); await clickAt(-26, 62);
  const picks = (await st()).sketchMode?.selection ?? [];
  pm = await at(0, 30); await page.mouse.move(pm[0], pm[1]); await page.waitForTimeout(200);
  const previewV = (await page.locator('[data-testid=snap-glyph]').textContent().catch(() => '')) ?? '';
  await clickAt(0, 30);
  await page.waitForSelector('[data-testid=dim-pending-input]', { timeout: 5000 });
  h = (await st()).hash;
  await page.locator('[data-testid=dim-pending-input]').press('Enter'); await waitHash(h);
  await page.keyboard.press('Escape'); await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  const toolOff = (await st()).sketchMode?.tool === null;
  check('two points placed beside the pair write the vertical distance, and escape clears the picks then stops the tool',
    picks.length === 2 && previewV === '62' && toolOff && /profile\.distance\("d1", "(bottom\.start|outer_wall\.end)", "(inner_wall\.end|top\.start)", 62, along="y", at=\(0, 30\)\)/.test(readFile()),
    `picks ${picks.join(',')}, preview ${previewV}, off ${toolOff}, ${readFile().match(/profile\.distance\("d1".*/)?.[0] ?? 'no statement'}`);

  // editing a dimension to an expression (with autocomplete available) writes the expression
  await page.locator('[data-testid=dim-len1]').click();
  await page.waitForSelector('[data-testid=dim-input-len1]', { timeout: 5000 });
  await page.locator('[data-testid=dim-input-len1]').fill('widt');
  await page.waitForTimeout(150);
  const suggest = await page.locator('.expr-suggest li').allTextContents();
  await page.locator('[data-testid=dim-input-len1]').press('Tab');
  await page.waitForTimeout(150);
  await page.locator('[data-testid=dim-input-len1]').type(' / 3', { delay: 30 });
  h = (await st()).hash;
  await page.locator('[data-testid=dim-input-len1]').press('Enter'); await waitHash(h);
  check('editing a dimension to an expression autocompletes names and writes the expression',
    suggest.includes('width') && /profile\.length\("len1", "line1", width \/ 3, at=\(50, 27\)\)/.test(readFile()) && (await page.locator('[data-testid=dim-len1]').textContent())?.includes('width / 3'),
    `suggest ${suggest.join(',')}, ${readFile().match(/profile\.length\("len1".*/)?.[0]}`);

  // dragging a label writes its new place into the file
  const lb = await page.locator('[data-testid=dim-len1]').boundingBox();
  await page.mouse.move(lb.x + lb.width / 2, lb.y + lb.height / 2); await page.mouse.down();
  for (let i = 1; i <= 6; i++) { await page.mouse.move(lb.x + lb.width / 2, lb.y + lb.height / 2 + 8 * i); await page.waitForTimeout(30); }
  h = (await st()).hash;
  await page.mouse.up(); await waitHash(h);
  const placedAt = readFile().match(/profile\.length\("len1", "line1", width \/ 3, at=\(([-\d.]+), ([-\d.]+)\)\)/);
  check('dragging a dimension label writes at= on its statement', placedAt !== null && Number(placedAt[2]) < 27 && Number(placedAt[2]) > 10,
    readFile().match(/profile\.length\("len1".*/)?.[0] ?? 'no statement');

  // construction geometry: toggle a profile line to construction (the profile opens, the extrude fails), then back
  await page.evaluate(() => window.__plainsolid.actions.setSketchSelection(['inner_bottom']));
  await page.waitForSelector('[data-testid=constrain-construction]', { timeout: 5000 });
  h = (await st()).hash;
  await page.click('[data-testid=constrain-construction]'); await waitHash(h);
  const openBody = (await st()).tree.features.find((f) => f.name === 'body');
  const wroteConstruction = /profile\.line\("inner_bottom", .*construction=True\)/.test(readFile());
  await page.evaluate(() => window.__plainsolid.actions.setSketchSelection(['inner_bottom']));
  await page.waitForSelector('[data-testid=constrain-construction].active', { timeout: 5000 });
  h = (await st()).hash;
  await page.click('[data-testid=constrain-construction]'); await waitHash(h);
  const s4 = await st();
  const closedBody = s4.tree.features.find((f) => f.name === 'body');
  const backEntity = s4.tree.features.find((f) => f.name === 'profile').entities.find((e) => e.name === 'inner_bottom');
  // the server's set_entity_argument keeps the keyword as construction=False rather than dropping it
  check('toggling a profile line to construction writes construction=True, opens the profile, and toggling back closes it',
    wroteConstruction && openBody?.result?.ok === false && !/inner_bottom".*construction=True/.test(readFile()) && backEntity?.construction === false && closedBody?.result?.ok === true,
    `wrote ${wroteConstruction}, body failed ${openBody?.result?.ok === false}, restored ${closedBody?.result?.ok} · ${readFile().match(/profile\.line\("inner_bottom".*/)?.[0]}`);

  // the construction switch: the x key turns it on, the active tool button goes dashed, a new line carries construction=True
  await page.locator('[data-testid=viewport] canvas').focus();
  await page.keyboard.press('x'); await page.waitForTimeout(150);
  const switchOn = await page.locator('[data-testid=sketch-construction-mode]').isChecked();
  await page.click('.sketch-bar .btn-small:has-text("line")');
  const lineDashed = (await page.locator('.sketch-bar .btn-small.active.construction').count()) === 1;
  const hintSaysConstruction = ((await page.locator('.sketch-hint').textContent()) ?? '').startsWith('construction');
  await clickAt(40, 30); h = (await st()).hash; await clickAt(60, 30.2); await waitHash(h);
  await page.keyboard.press('Escape'); await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  const stillOn = await page.locator('[data-testid=sketch-construction-mode]').isChecked();  // stopping the tool leaves the switch alone
  await page.click('[data-testid=sketch-construction-mode]'); await page.waitForTimeout(100);
  const switchOff = !(await page.locator('[data-testid=sketch-construction-mode]').isChecked());
  const cLine = (await st()).tree.features.find((f) => f.name === 'profile').entities.find((e) => e.name === 'line2');
  check('the construction switch (x) marks the active tool dashed and writes construction=True with the inferred constraint',
    switchOn && lineDashed && hintSaysConstruction && stillOn && switchOff
      && /profile\.line\("line2", \(40, 30\), \(60, 30\), construction=True\)/.test(readFile()) && /profile\.horizontal\("h\d+", "line2"\)/.test(readFile()) && cLine?.construction === true,
    `on ${switchOn}, dashed ${lineDashed}, hint ${hintSaysConstruction}, stayed ${stillOn}, off ${switchOff} · ${readFile().match(/profile\.line\("line2".*/)?.[0] ?? 'no line2'}`);

  // projection: in the holes sketch, click the body's top face
  await page.click('.sketch-bar .btn-small:has-text("exit sketch")');
  await page.waitForTimeout(300);
  await page.click('[data-testid=feature-holes]');
  await page.click('.btn:has-text("edit sketch")');
  await page.waitForSelector('[data-testid=sketch-bar]');
  await page.waitForFunction(() => window.__plainsolid.getState().mesh?.header.upto === 'outer', null, { timeout: 15000 });
  await page.waitForTimeout(500);
  await page.click('[data-testid=sketch-project]');
  await page.waitForTimeout(200);
  let projHit = false;
  for (const [fx, fy] of [[0.5, 0.5], [0.55, 0.55], [0.45, 0.45], [0.5, 0.6], [0.6, 0.5]]) {
    await page.mouse.move(vp.x + vp.width * fx, vp.y + vp.height * fy); await page.waitForTimeout(250);
    if ((await st()).hover) { projHit = true; break; }
  }
  h = (await st()).hash;
  await page.mouse.down(); await page.mouse.up();
  await waitHash(h).catch(() => {});
  const convertTool = (await st()).sketchMode?.tool;
  check('convert writes a project statement with a semantic or nearest() selector as real geometry and stays on',
    projHit && /holes\.project\("(face|edge|vertex)1", (body|inner|outer)\.(faces|edges|vertices)\.(top|bottom|from_sketch\("[\w.]+"\)|nearest\(\(-?[\d.]+, -?[\d.]+, -?[\d.]+\)\))[^\n]*, construction=False\)/.test(readFile()) && convertTool === 'project',
    `${readFile().match(/holes\.project\(.*/)?.[0] ?? `hover ${projHit}`} · tool ${convertTool}`);
  await page.screenshot({ path: 'screenshot-sketch-drag.png' });
  await page.keyboard.press('Escape');
  await page.click('[data-testid=sketch-exit]');
  await page.waitForTimeout(300);
}

// ---- review tools -----------------------------------------------------------------
const state = () => page.evaluate(() => window.__plainsolid.getState());
const VIEWS = path.join(PROJ, 'node_review.views.json');
const readViews = () => (fs.existsSync(VIEWS) ? JSON.parse(fs.readFileSync(VIEWS, 'utf8')) : null);

// switch to the assembly document
await page.click('[data-testid="doc-tab-node_review"]');
await page.waitForSelector('[data-testid="instance-node.glands.gland_1"]', { timeout: 20000 });
await page.waitForFunction(() => window.__plainsolid.getState().mesh?.header.items?.length === 4, null, { timeout: 20000 });
await page.waitForTimeout(500);
const leafRows = await page.locator('[data-testid^="instance-"]').count();
const s1 = await state();
const coloured = s1.mesh.header.items.filter((it) => it.color).length;
check('assembly shows the instance tree with four coloured leaves', leafRows === 6 && coloured === 4, `rows ${leafRows}, coloured ${coloured}`);

// click an instance selects it and shows its product in the panel
await page.click('[data-testid="instance-node.bracket"]');
await page.waitForTimeout(200);
check('instance selection shows product and solids', (await page.locator('[data-testid=instance-props]').textContent())?.includes('bracket') ?? false);

// a sub-assembly of the file opens in a tab of its own through a wrapper named after the node, in its own coordinates
await page.click('[data-testid="instance-node.glands"]', { button: 'right' });
await page.waitForSelector('[data-testid=ctx-open-sub-assembly]');
await page.click('[data-testid=ctx-open-sub-assembly]');
await page.waitForSelector('[data-testid="doc-tab-glands"]', { timeout: 30000 });
await page.waitForSelector('[data-testid="instance-glands.gland_2"]', { timeout: 30000 });
await page.waitForFunction(() => window.__plainsolid.getState().mesh?.header.items?.length === 2, null, { timeout: 20000 });
const subWrapper = path.join(PROJ, 'vendor', 'node_stub.glands.py');
check('a sub-assembly of a STEP viewer opens in a tab of its own through a wrapper named after the node',
  ((await page.locator('.doc-tab.active').textContent()) ?? '').includes('glands') && fs.existsSync(subWrapper)
    && fs.readFileSync(subWrapper, 'utf8').includes('glands = import_step("glands", "node_stub.step#node.glands")')
    && (await page.locator('[data-testid^="instance-"]').count()) === 3
    && (await page.locator('[data-testid=ctx-open-sub-assembly]').count()) === 0,
  fs.existsSync(subWrapper) ? fs.readFileSync(subWrapper, 'utf8').split('\n').pop() : 'no wrapper');
await page.click('[data-testid="instance-glands.gland_1"]', { button: 'right' });
await page.waitForSelector('[data-testid=context-menu]');
const subRowMenu = await (await state()).contextMenu?.entries.filter((e) => !e.sep).map((e) => e.label);
await page.keyboard.press('Escape');
check('inside the sub-assembly its root is this document, so a body offers no parent to open', !(subRowMenu ?? []).some((l) => l.startsWith('open ')), (subRowMenu ?? []).join(' | '));
await page.click('.doc-tab.active .doc-tab-close');
await page.waitForTimeout(500);
await page.click('[data-testid="doc-tab-node_review"]');
await page.waitForSelector('[data-testid="instance-node.glands.gland_1"]', { timeout: 20000 });
await page.click('[data-testid="instance-node.bracket"]');
await page.waitForTimeout(200);

// visibility toggle persists to the sidecar
await page.locator('[data-testid="vis-node.bracket"]').click();
await page.waitForTimeout(1200);
const v1 = readViews();
check('visibility toggle hides an item and persists', v1?.visibility?.['node.bracket'] === false, JSON.stringify(v1?.visibility));
await page.locator('[data-testid="vis-node.bracket"]').click();
await page.waitForTimeout(400);

// section XY at offset 5 refetches a capped mesh
await page.click('[data-testid=tool-section]');
await page.waitForSelector('[data-testid=section-panel]');
await page.click('[data-testid=section-panel] .btn-small:has-text("XY")');
await page.waitForFunction(() => window.__plainsolid.getState().mesh?.header.section?.plane === 'XY', null, { timeout: 15000 });
const off = page.locator('[data-testid=section-offset]');
await off.fill('5'); await off.press('Enter');
await page.waitForFunction(() => window.__plainsolid.getState().mesh?.header.section?.offset === 5, null, { timeout: 15000 });
await page.waitForTimeout(300);
const s2 = await state();
check('section XY offset 5 refetches a mesh with cap faces', s2.mesh.header.section_faces.length > 0 && s2.mesh.header.bbox[1][2] <= 5.01, `caps ${s2.mesh.header.section_faces.length}, top z ${s2.mesh.header.bbox[1][2]}`);
const installsBefore = s2.sectionInstalls;

// a burst of commits is latest-wins: offsets 8, 10 typed in quick succession -> one installed mesh at 10
await off.fill('8'); await off.press('Enter');
await off.fill('10'); await off.press('Enter');
await page.waitForFunction(() => { const s = window.__plainsolid.getState(); return !s.sectionPending && s.mesh?.header.section?.offset === 10; }, null, { timeout: 15000 });
await page.waitForTimeout(400);
const s2b = await state();
check('quick section commits end with exactly one installed mesh at the last offset',
  s2b.sectionInstalls === installsBefore + 1 && s2b.mesh.header.section.offset === 10 && s2b.mesh.header.bbox[1][2] <= 10.01,
  `installs +${s2b.sectionInstalls - installsBefore}, offset ${s2b.mesh.header.section?.offset}, top z ${s2b.mesh.header.bbox[1][2]}`);

// the GPU clip covers the wait: 1 plane right after a commit, 0 once the capped mesh is installed
const clipDuring = await page.evaluate(async () => {
  const p = window.__plainsolid;
  p.actions.setSection({ plane: 'XY', offset: 7, flip: false });
  await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));
  return { clips: p.clipCount(), pending: p.getState().sectionPending, installed: p.getState().mesh?.header.section?.offset ?? null };
});
await page.waitForFunction(() => { const s = window.__plainsolid.getState(); return !s.sectionPending && s.mesh?.header.section?.offset === 7; }, null, { timeout: 15000 });
await page.waitForTimeout(300);
const clipAfter = await page.evaluate(() => window.__plainsolid.clipCount());
check('the clip stays on while the capped mesh is fetched and goes off once installed',
  clipDuring.clips === 1 && clipAfter === 0, `during ${JSON.stringify(clipDuring)}, after ${clipAfter}`);
check('section status shows the fetch time', /\d s/.test((await page.locator('[data-testid=section-status]').textContent()) ?? ''),
  (await page.locator('[data-testid=section-status]').textContent()) ?? '');

// turning the section off restores the uncut mesh at once
await page.click('[data-testid=section-clear]');
await page.waitForTimeout(300);
const s2c = await state();
const clipOff = await page.evaluate(() => window.__plainsolid.clipCount());
check('clearing the section restores the uncut mesh', s2c.section === null && !s2c.mesh.header.section && s2c.mesh.header.bbox[1][2] > 30 && clipOff === 0,
  `top z ${s2c.mesh.header.bbox[1][2]}, clips ${clipOff}`);

// back to the section the rest of the checks rely on
await page.click('[data-testid=section-panel] .btn-small:has-text("XY")');
await off.fill('5'); await off.press('Enter');
await page.waitForFunction(() => { const s = window.__plainsolid.getState(); return !s.sectionPending && s.mesh?.header.section?.offset === 5; }, null, { timeout: 15000 });
await page.waitForTimeout(1200);
check('section persists to the sidecar', readViews()?.section?.offset === 5);

// measure two points, pin it
await page.click('[data-testid=tool-measure]');
await page.waitForSelector('[data-testid=measure-panel]');
await page.fill('[data-testid=measure-a]', '0,0,0');
await page.fill('[data-testid=measure-b]', '3,4,0');
await page.click('[data-testid=measure-go]');
await page.waitForSelector('[data-testid=measure-result]', { timeout: 15000 });
const dist = await page.locator('.measure-distance').textContent();
check('measuring two points returns the distance', dist?.trim().startsWith('5') ?? false, dist ?? '');
await page.click('[data-testid=measure-pin]');
await page.waitForTimeout(1200);
const v2 = readViews();
check('a pinned measurement persists to the views file', (await page.locator('[data-testid=pin-row]').count()) === 1 && v2?.pins?.length === 1, JSON.stringify(v2?.pins?.[0]?.text));
// measure two faces by id
await page.fill('[data-testid=measure-a]', 'face:0');
await page.fill('[data-testid=measure-b]', 'face:7');
await page.click('[data-testid=measure-go]');
await page.waitForFunction(() => document.querySelector('[data-testid=measure-result]')?.textContent?.includes('b:'), null, { timeout: 15000 });
const s3 = await state();
check('measuring two faces returns a distance', typeof s3.measure.result?.distance === 'number', `distance ${s3.measure.result?.distance}`);
// pick a face in the viewport while measuring: hover shows the entity kind
await page.mouse.move(vp.x + vp.width * 0.5, vp.y + vp.height * 0.5);
await page.waitForTimeout(300);
const hoverText = await page.locator('.viewport-hover').textContent().catch(() => '');
check('measure hover reports an entity', /(face|edge|vertex) \d+/.test(hoverText ?? ''), hoverText ?? '');
await page.screenshot({ path: 'screenshot-measure.png' });
await page.click('[data-testid=measure-pin]');
await page.waitForTimeout(400);

// named view: save (prompt answers XZ), move the camera, restore
await page.click('.btn-small:has-text("iso")');
await page.waitForTimeout(300);
const camSaved = (await state()).camera;
await page.selectOption('[data-testid=views-select]', '__save');
await page.waitForTimeout(1200);
check('a named view saves to the sidecar', Boolean(readViews()?.named?.XZ), Object.keys(readViews()?.named ?? {}).join(','));
await page.click('.btn-small:has-text("top")');
await page.waitForTimeout(300);
const camMoved = (await state()).camera;
await page.selectOption('[data-testid=views-select]', 'XZ');
await page.waitForTimeout(600);
const camBack = (await state()).camera;
const same = (a, b) => a && b && a.position.every((v, i) => Math.abs(v - b.position[i]) < 1e-6);
check('restoring the named view restores the camera', !same(camSaved, camMoved) && same(camSaved, camBack));
await page.screenshot({ path: 'screenshot-assembly.png' });

// open a STEP file directly: the server writes a wrapper
await page.click('[data-testid=open-menu]');
await page.fill('[data-testid=open-path]', 'vendor/node_stub.step');
await page.press('[data-testid=open-path]', 'Enter');
await page.waitForSelector('[data-testid="instance-node_stub.box"]', { timeout: 20000 });
check('opening a STEP path creates the wrapper and shows its tree', fs.existsSync(path.join(PROJ, 'vendor', 'node_stub.py')));
await page.evaluate(() => window.__plainsolid.actions.select(null));
await page.waitForTimeout(200);
check('a STEP viewer with nothing selected shows the viewer panel, not a bill of materials', (await page.locator('[data-testid=viewer-props]').count()) === 1 && (await page.locator('[data-testid=bom-table]').count()) === 0);
await page.click('[data-testid="vis-node_stub.box"]', { modifiers: ['Alt'] });
await page.waitForTimeout(200);
{
  const vis = await page.evaluate(() => window.__plainsolid.getState().visibility);
  const only = vis['node_stub.box'] === true && Object.entries(vis).filter(([k, v]) => k !== 'node_stub.box' && v === false).length >= 1;
  await page.click('[data-testid=show-all]');
  await page.waitForTimeout(200);
  const all = await page.evaluate(() => Object.keys(window.__plainsolid.getState().visibility).length === 0);
  check('alt+click on a visibility checkbox isolates the body and show all brings the rest back', only && all, JSON.stringify(vis));
}

// create a new part from scratch: the file appears with metadata only and becomes the current document
await page.click('[data-testid=new-menu]');
await page.click('[data-testid=new-part]');
await page.fill('[data-testid=new-name]', 'parts/fresh_part');
await page.click('[data-testid=new-create]');
await page.waitForFunction(() => document.querySelector('.doc-tab.active')?.textContent?.includes('fresh_part'), null, { timeout: 20000 });
await page.waitForFunction(() => window.__plainsolid.getState().tree?.features?.length === 0, null, { timeout: 20000 }).catch(() => {});
const freshSrc = fs.existsSync(path.join(PROJ, 'parts', 'fresh_part.py')) ? fs.readFileSync(path.join(PROJ, 'parts', 'fresh_part.py'), 'utf8') : '';
check('new part creates a template file and selects it',
  freshSrc.startsWith('from plainsolid import *') && freshSrc.includes('meta(name="fresh_part"') && (await page.locator('.tree-row').count()) === 0,
  freshSrc.split('\n')[2] || '');

// a new assembly in a subfolder: an instance chosen from the project list is written relative to the assembly file
await page.click('[data-testid=new-menu]');
await page.click('[data-testid=new-assembly]');
await page.fill('[data-testid=new-name]', 'parts/fresh_asm');
await page.click('[data-testid=new-create]');
await page.waitForFunction(() => document.querySelector('.doc-tab.active')?.textContent?.includes('fresh_asm') && window.__plainsolid.getState().tree?.kind === 'assembly', null, { timeout: 20000 });
await page.waitForSelector('[data-testid=new-instance]');
await page.click('[data-testid=new-instance]');
await page.waitForSelector('[data-testid=instance-path]');
await page.fill('[data-testid=instance-path]', 'bracket.py');
{
  const h0 = await page.evaluate(() => window.__plainsolid.getState().hash);
  await page.click('[data-testid=feature-ok]');
  await page.waitForFunction((h) => { const s = window.__plainsolid.getState(); return s.hash !== h && s.tree?.hash === s.hash; }, h0, { timeout: 20000 });
  await page.waitForTimeout(400);
}
const asmSrc = fs.readFileSync(path.join(PROJ, 'parts', 'fresh_asm.py'), 'utf8');
check('new assembly creates an assembly file and an instance picked from the project list is relative to it',
  asmSrc.includes('meta(kind="assembly", name="fresh_asm"') && asmSrc.includes('bracket = instance("bracket", "../bracket.py")')
    && (await page.evaluate(() => window.__plainsolid.getState().tree?.features?.[0]?.result?.ok)) === true,
  asmSrc.split('\n').filter((l) => l.includes('instance(')).join(' | ') || asmSrc.slice(0, 80));

// ---- reference planes, sketches on faces, confirmed cascade delete -----------------------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  const waitHash = async (h) => page.waitForFunction((h) => { const s = window.__plainsolid.getState(); return s.hash !== h && s.tree?.hash === s.hash; }, h, { timeout: 15000 }).then(() => page.waitForTimeout(700));
  await page.click('[data-testid="doc-tab-bracket"]');
  await page.waitForSelector('[data-testid=feature-profile]', { timeout: 20000 });
  await page.waitForTimeout(800);

  // delete the profile sketch: the popover names the extrude and the entity count; the cascade leaves a valid file
  await page.click('[data-testid=feature-profile]');
  await page.click('[data-testid=delete-feature]');
  const confirmText = (await page.locator('[data-testid=delete-confirm]').textContent().catch(() => '')) ?? '';
  let h = (await st()).hash;
  await page.click('[data-testid=delete-confirm-ok]');
  await waitHash(h);
  const afterDelete = readFile();
  const s1 = await st();
  check('deleting a sketch asks first and cascades to its entities and dependants, leaving a valid file',
    /body/.test(confirmText) && /\d+ sketch entit/.test(confirmText) && !/profile\./.test(afterDelete) && !/extrude\("body"/.test(afterDelete)
      && s1.tree.errors.length === 0 && !s1.tree.features.some((f) => f.name === 'profile' || f.name === 'body'),
    `confirm: ${confirmText.slice(0, 70)} · errors ${s1.tree.errors.length} · features ${s1.tree.features.map((f) => f.name).join(',')}`);
  await page.locator('[data-testid=viewport] canvas').focus();
  h = (await st()).hash;
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+z' : 'Control+z');
  await waitHash(h);
  check('undo brings the sketch and its dependants back', /profile\.line\("bottom"/.test(readFile()) && /extrude\("body"/.test(readFile()));

  // + plane: offset 25 from XY through the dialog
  await page.click('[data-testid=new-plane]');
  await page.waitForSelector('[data-testid=plane-dialog]');
  await page.selectOption('[data-testid=plane-base-select]', 'XY');
  await page.fill('[data-testid=plane-offset]', '25');
  await page.press('[data-testid=plane-offset]', 'Enter');
  h = (await st()).hash;
  await page.click('[data-testid=plane-ok]');
  await waitHash(h);
  const s2 = await st();
  const plane1 = s2.tree.features.find((f) => f.name === 'plane1');
  const planeCount = await page.evaluate(() => window.__plainsolid.planeCount());
  check('+ plane offset 25 from XY writes the statement and draws the plane square',
    /plane1 = plane\("plane1", XY, offset=25\)/.test(readFile()) && plane1?.result?.plane?.origin?.[2] === 25 && planeCount >= 4,
    `${readFile().match(/plane1 = .*/)?.[0] ?? 'no statement'} · squares ${planeCount}`);
  await page.screenshot({ path: 'screenshot-planes.png' });

  // + sketch on the base's top face: select the lowest +Z face in the viewport, then + sketch
  const topFace = await page.evaluate(() => {
    const m = window.__plainsolid.getState().mesh;
    let best = null;
    m.header.face_ranges.forEach(([t0, tn], fid) => {
      if (!tn) return;
      let nz = 0, z = 0, n = 0;
      for (let t = t0; t < t0 + tn; t++) for (let k = 0; k < 3; k++) { const v = m.indices[3 * t + k]; nz += m.normals[3 * v + 2]; z += m.positions[3 * v + 2]; n++; }
      nz /= n; z /= n;
      if (nz > 0.99 && (best === null || z < best.z)) best = { fid, z };
    });
    return best;
  });
  await page.evaluate((fid) => window.__plainsolid.actions.selectFace(fid), topFace.fid);
  await page.waitForTimeout(200);
  h = (await st()).hash;
  await page.click('[data-testid=new-sketch]');
  await waitHash(h);
  await page.waitForSelector('[data-testid=sketch-bar]', { timeout: 15000 });
  await page.waitForTimeout(600);
  const s3 = await st();
  const faceSketch = readFile().match(/(sketch\d+) = sketch\("sketch\d+", on=(?:body|inner|outer)\.faces\.(?:from_sketch\("\w+"\)|top|bottom|nearest\(\(.*\)\))\)/);
  check('+ sketch with a face selected writes a face selector (semantic when the face has a unique label) and opens sketch mode on that face',
    faceSketch !== null && s3.sketchMode !== null && Math.abs(s3.sketchMode.frame.origin[2] - topFace.z) < 1e-3 && Math.abs(s3.sketchMode.frame.z_dir[2] - 1) < 1e-6,
    `${faceSketch?.[0] ?? 'no statement'} · frame z ${s3.sketchMode?.frame?.origin?.[2]} (face z ${topFace?.z})`);
  await page.screenshot({ path: 'screenshot-face-sketch.png' });

  // draw a circle on the face sketch, then extrude it: the volume grows
  const sketchName = faceSketch?.[1];
  const at = (u, v) => page.evaluate(([u, v]) => window.__plainsolid.sketchToScreen(u, v), [u, v]);
  const clickAt = async (u, v) => { const p = await at(u, v); await page.mouse.move(p[0], p[1]); await page.waitForTimeout(150); await page.mouse.down(); await page.mouse.up(); await page.waitForTimeout(250); };
  const docId = s3.docId;
  const volume = () => page.evaluate(async (id) => (await (await fetch(`/api/documents/${id}/query/volume`)).json()).volume, docId);
  const v0 = await volume();
  await page.click('.sketch-bar .btn-small:has-text("circle")');
  await clickAt(0, 5); h = (await st()).hash; await clickAt(3, 5); await waitHash(h);
  await page.keyboard.press('Escape'); await page.keyboard.press('Escape'); await page.waitForTimeout(300);
  const ghostBefore = await page.evaluate(() => window.__plainsolid.hasGhost());
  h = (await st()).hash;
  await page.click('[data-testid=add-extrude]');  // still in the sketch: the panel offers extrude there too, as a dialog with a preview
  await page.waitForSelector('[data-testid=feature-dialog]');
  await page.waitForFunction(() => window.__plainsolid.hasGhost() && window.__plainsolid.getState().ghostStyle === 'preview', null, { timeout: 15000 }).catch(() => {});
  const ghostAfter = await page.evaluate(() => window.__plainsolid.hasGhost());
  const stillSketching = (await st()).sketchMode !== null && (await st()).hash === h;
  check('extruding from inside the sketch previews the new boss over the pre-sketch body before anything is written', !ghostBefore && ghostAfter && stillSketching,
    `ghost ${ghostBefore} -> ${ghostAfter}`);
  await page.click('[data-testid=feature-ok]');
  await waitHash(h);
  await page.waitForFunction(() => window.__plainsolid.getState().sketchMode === null, null, { timeout: 15000 }).catch(() => {});
  const v1 = await volume();
  check('adding the extrude writes it after the sketch, closes the sketch and the volume grows',
    new RegExp(`${sketchName}\\.circle\\("circle\\d+", 6, at=\\(0, 5\\)\\)`).test(readFile()) && v1 > v0 + 1 && (await st()).sketchMode === null && (await st()).selected?.startsWith('extrude'),
    `volume ${v0?.toFixed(1)} -> ${v1?.toFixed(1)} · ${readFile().match(new RegExp(`${sketchName}\\.circle.*`))?.[0] ?? 'no circle'} · selected ${(await st()).selected}`);
}

// ---- fillet dialog with a semantic selector, suppression, mirror dialog, identity tags --------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  const waitHash = async (h) => page.waitForFunction((h) => { const s = window.__plainsolid.getState(); return s.hash !== h && s.tree?.hash === s.hash; }, h, { timeout: 15000 }).then(() => page.waitForTimeout(700));
  await page.keyboard.press('Escape');
  await page.click('[data-testid=feature-body]');
  await page.waitForTimeout(200);

  // the identity map reaches the client: the body's faces carry top/bottom and profile-line tags
  const tagged = await page.evaluate(() => {
    const h = window.__plainsolid.getState().mesh.header;
    const tags = Object.values(h.face_tags ?? {}).flat();
    const find = (a, b) => Object.entries(h.edge_tags ?? {}).find(([, t]) => t.length === 2 && t.includes(a) && t.includes(b));
    const edge = find(':top', 'bottom'), edge2 = find(':top', 'right');
    return { hasTop: tags.includes(':top'), hasWall: tags.includes('outer_wall'), edgeId: edge ? Number(edge[0]) : null, edge2Id: edge2 ? Number(edge2[0]) : null };
  });
  check('mesh header carries identity tags for faces and edges', tagged.hasTop && tagged.hasWall && tagged.edgeId !== null && tagged.edge2Id !== null, JSON.stringify(tagged));

  // + feature > fillet: pick two edges by id one after another (the picker stays armed), unpick and
  // re-pick the second, radius 2, add: the file gets a list of semantic selectors
  await page.click('[data-testid=new-feature]');
  await page.click('[data-testid=add-fillet]');
  await page.waitForSelector('[data-testid=feature-dialog]');
  await page.waitForFunction(() => window.__plainsolid.getState().pickRequest !== null, null, { timeout: 5000 });  // armed on open
  await page.evaluate((id) => window.__plainsolid.actions.pickEntity('edge', id), tagged.edgeId);
  await page.waitForTimeout(150);
  await page.evaluate((id) => window.__plainsolid.actions.pickEntity('edge', id), tagged.edge2Id);
  await page.waitForTimeout(150);
  const chipsAfterTwo = await page.locator('.pick-chip').count();
  const litAfterTwo = (await st()).dialogPicks.length;
  await page.evaluate((id) => window.__plainsolid.actions.pickEntity('edge', id), tagged.edge2Id);
  await page.waitForTimeout(150);
  const chipsAfterUnpick = await page.locator('.pick-chip').count();
  await page.evaluate((id) => window.__plainsolid.actions.pickEntity('edge', id), tagged.edge2Id);
  await page.waitForTimeout(150);
  const chips = await page.locator('.pick-chip').count();
  const stillArmed = (await st()).pickRequest !== null;
  await page.fill('[data-testid=feature-radius]', '2');
  let h = (await st()).hash;
  await page.click('[data-testid=feature-ok]');
  await waitHash(h);
  await page.waitForFunction(() => window.__plainsolid.getState().selected === 'fillet1', null, { timeout: 5000 }).catch(() => {});
  const s4 = await st();
  const filletStmt = readFile().match(/fillet1 = fillet\(.*/)?.[0] ?? '';
  const fillet1 = s4.tree.features.find((f) => f.name === 'fillet1');
  check('fillet dialog gathers several edges, unpicks on a second click, and writes a list of semantic selectors that evaluates',
    chipsAfterTwo === 2 && litAfterTwo === 2 && chipsAfterUnpick === 1 && chips === 2 && stillArmed
      && /fillet1 = fillet\("fillet1", \[body\.edges\.top\.from_sketch\("bottom"\), body\.edges\.top\.from_sketch\("right"\)\], 2\)/.test(filletStmt)
      && fillet1?.result?.ok === true && s4.selected === 'fillet1' && s4.pickRequest === null,
    `${filletStmt || 'no statement'} · ok ${fillet1?.result?.ok} · chips ${chipsAfterTwo}/${chipsAfterUnpick}/${chips} · armed ${stillArmed} · selected ${s4.selected} · request ${s4.pickRequest !== null} · dialog ${s4.featureDialog !== null}`);
  await page.screenshot({ path: 'screenshot-fillet.png' });

  // suppress it from the property panel, then unsuppress: the keyword comes and goes
  h = s4.hash;
  await page.click('[data-testid=suppress]');
  await waitHash(h);
  const suppressed = /fillet1 = fillet\(.*, suppressed=True\)/.test(readFile());
  const badge = await page.locator('[data-testid=feature-fillet1] .badge', { hasText: 'off' }).count();
  h = (await st()).hash;
  await page.click('[data-testid=suppress]');
  await waitHash(h);
  check('suppress checkbox writes suppressed=True, shows the off badge, and unchecking drops the keyword',
    suppressed && badge === 1 && !/suppressed=/.test(readFile()), readFile().match(/fillet1 = .*/)?.[0] ?? '');

  // mirror the base extrude across XZ from its property panel
  await page.click('[data-testid=feature-body]');
  await page.waitForTimeout(200);
  await page.click('[data-testid=repeat-buttons] button:has-text("mirror")');
  await page.waitForSelector('[data-testid=feature-dialog]');
  await page.selectOption('[data-testid=feature-about]', 'XZ');
  const volBefore = await page.evaluate(async (id) => (await (await fetch(`/api/documents/${id}/query/volume`)).json()).volume, s4.docId);
  h = (await st()).hash;
  await page.click('[data-testid=feature-ok]');
  await waitHash(h);
  const volAfter = await page.evaluate(async (id) => (await (await fetch(`/api/documents/${id}/query/volume`)).json()).volume, s4.docId);
  const mirrorStmt = readFile().match(/mirror1 = mirror\(.*/)?.[0] ?? '';
  check('mirror dialog writes mirror("mirror1", body, about=XZ) and the body grows',
    /mirror1 = mirror\("mirror1", body, about=XZ\)/.test(mirrorStmt) && volAfter > volBefore * 1.5, `${mirrorStmt || 'no statement'} · volume ${volBefore?.toFixed(0)} -> ${volAfter?.toFixed(0)}`);

  // hovering a face shows its identity tags in the viewport label
  const vp2 = await page.locator('[data-testid=viewport] canvas').boundingBox();
  let hoverText = '';
  for (const [fx, fy] of [[0.5, 0.62], [0.5, 0.7], [0.4, 0.5], [0.6, 0.6], [0.5, 0.5], [0.45, 0.4]]) {
    await page.mouse.move(vp2.x + vp2.width * fx, vp2.y + vp2.height * fy);
    await page.waitForTimeout(200);
    hoverText = (await page.locator('.viewport-hover').textContent().catch(() => '')) ?? '';
    if (/face \d+ · \w+ · \S+/.test(hoverText)) break;
  }
  check('hovering a face shows its owner and identity tags', /face \d+ · \w+ · \S+/.test(hoverText), hoverText);
}

// ---- orthographic view, sketch view, relations to the body, offset, angle -------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  const waitHash = async (h) => page.waitForFunction((h) => { const s = window.__plainsolid.getState(); return s.hash !== h && s.tree?.hash === s.hash; }, h, { timeout: 15000 }).then(() => page.waitForTimeout(700));
  const at = (u, v) => page.evaluate(([u, v]) => window.__plainsolid.sketchToScreen(u, v), [u, v]);
  const clickAt = async (u, v) => { const p = await at(u, v); await page.mouse.move(p[0], p[1]); await page.waitForTimeout(150); await page.mouse.down(); await page.mouse.up(); await page.waitForTimeout(250); };
  await page.keyboard.press('Escape');

  // the ortho toggle
  const orthoStart = await page.evaluate(() => window.__plainsolid.isOrtho());
  await page.click('[data-testid=toggle-ortho]');
  await page.waitForTimeout(200);
  const orthoFlipped = await page.evaluate(() => window.__plainsolid.isOrtho()) === !orthoStart && (await st()).ortho === !orthoStart;
  await page.click('[data-testid=toggle-ortho]');
  await page.waitForTimeout(200);
  const orthoBack = await page.evaluate(() => window.__plainsolid.isOrtho()) === orthoStart;
  check('the ortho button switches the projection and back, and no sketch left it on', orthoFlipped && orthoBack && !orthoStart, `start ${orthoStart}`);

  // a face selected: the header keeps its labels and a caption says where the next sketch goes
  const topFace2 = await page.evaluate(() => {
    const m = window.__plainsolid.getState().mesh;
    let best = null;
    m.header.face_ranges.forEach(([t0, tn], fid) => {
      if (!tn) return;
      let nz = 0, z = 0, n = 0;
      for (let t = t0; t < t0 + tn; t++) for (let k = 0; k < 3; k++) { const v = m.indices[3 * t + k]; nz += m.normals[3 * v + 2]; z += m.positions[3 * v + 2]; n++; }
      nz /= n; z /= n;
      if (nz > 0.99 && (best === null || z > best.z)) best = { fid, z };
    });
    return best;
  });
  await page.evaluate((fid) => window.__plainsolid.actions.selectFace(fid), topFace2.fid);
  await page.waitForTimeout(200);
  const captionText = (await page.locator('[data-testid=sketch-target]').textContent().catch(() => '')) ?? '';
  const sketchBtn = (await page.locator('[data-testid=new-sketch]').textContent()) ?? '';
  check('a selected face shows as a caption under fixed header buttons', /face \d+/.test(captionText) && sketchBtn.trim() === '+ sketch', `${captionText} · ${sketchBtn}`);
  await page.evaluate(() => window.__plainsolid.actions.selectFace(null));

  // entering a sketch goes orthographic and leaving restores perspective; escape stays inside
  await page.click('[data-testid=feature-profile]');
  await page.click('.btn:has-text("edit sketch")');
  await page.waitForSelector('[data-testid=sketch-bar]');
  await page.waitForTimeout(500);
  const inSketchOrtho = await page.evaluate(() => window.__plainsolid.isOrtho());
  await page.keyboard.press('Escape'); await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  const stayed = (await st()).sketchMode !== null;
  check('a sketch opens in orthographic projection and escape keeps it open', inSketchOrtho && stayed);

  // two lines at an angle: the panel's selected section offers an angle dimension
  await page.evaluate(() => window.__plainsolid.actions.setSketchSelection(['line1', 'right']));
  await page.waitForTimeout(200);
  const dimLabel = (await page.locator('[data-testid=constrain-dimension]').textContent().catch(() => '')) ?? '';
  check('two lines at an angle offer an angle dimension in the panel', dimLabel.trim() === 'angle', dimLabel);

  // offset the L loop outward by 3 from the right-click menu: click outside the loop for the side, type the distance where you clicked
  await page.evaluate(() => window.__plainsolid.actions.setSketchSelection(['bottom', 'right', 'inner_bottom', 'inner_wall', 'top', 'outer_wall']));
  const pOff = await at(10, 0); await page.mouse.click(pOff[0], pOff[1], { button: 'right' });
  await page.waitForSelector('[data-testid=ctx-offset]', { timeout: 5000 });
  await page.click('[data-testid=ctx-offset]');
  await page.waitForTimeout(150);
  await clickAt(-45, -10);
  await page.waitForSelector('[data-testid=offset-pending-input]', { timeout: 5000 });
  await page.fill('[data-testid=offset-pending-input]', '3');
  let h = (await st()).hash;
  await page.locator('[data-testid=offset-pending-input]').press('Enter');
  await waitHash(h);
  const offsetStmt = readFile().match(/profile\.offset\(.*/)?.[0] ?? '';
  const sOff = await st();
  const offsetEntity = sOff.tree.features.find((f) => f.name === 'profile')?.entities.find((e) => e.kind === 'offset');
  const offsetItems = sOff.tree.features.find((f) => f.name === 'profile')?.result?.sketch?.projected?.offset1?.items?.length ?? 0;
  check('the offset tool writes an offset of the six selected lines, outside, and it resolves to six curves',
    /profile\.offset\("offset1", \["bottom", "right", "inner_bottom", "inner_wall", "top", "outer_wall"\], 3\)/.test(offsetStmt) && offsetEntity?.args?.side === 'outside' && offsetItems === 6,
    `${offsetStmt || 'no statement'} · items ${offsetItems}`);
  await page.screenshot({ path: 'screenshot-offset.png' });
  await page.locator('[data-testid=viewport] canvas').focus();
  h = (await st()).hash;
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+z' : 'Control+z');
  await waitHash(h);

  // rotate the sketch view with alt+drag, then normal to brings it back square on
  const vp3 = await page.locator('[data-testid=viewport] canvas').boundingBox();
  const camBefore = (await st()).camera;
  await page.keyboard.down('Alt');
  await page.mouse.move(vp3.x + vp3.width * 0.7, vp3.y + vp3.height * 0.7);
  await page.mouse.down(); await page.mouse.move(vp3.x + vp3.width * 0.5, vp3.y + vp3.height * 0.6, { steps: 8 }); await page.mouse.up();
  await page.keyboard.up('Alt');
  await page.waitForTimeout(300);
  const camRotated = (await st()).camera;
  // "normal to" lives in the right-click menu on empty plane (and on ctrl+0)
  await page.mouse.click(vp3.x + vp3.width * 0.15, vp3.y + vp3.height * 0.85, { button: 'right' });
  await page.waitForSelector('[data-testid=ctx-normal-to]', { timeout: 5000 });
  await page.click('[data-testid=ctx-normal-to]');
  await page.waitForTimeout(300);
  const camNormal = (await st()).camera;
  const dir = (c) => { const d = [c.position[0] - c.target[0], c.position[1] - c.target[1], c.position[2] - c.target[2]]; const n = Math.hypot(...d); return d.map((x) => x / n); };
  const moved = camBefore && camRotated && Math.abs(dir(camBefore)[1] - dir(camRotated)[1]) > 0.05;
  const back = camNormal && Math.abs(Math.abs(dir(camNormal)[1]) - 1) < 1e-3;
  check('alt+drag orbits inside a sketch and "normal to" looks at the plane again', moved && back, `before ${dir(camBefore ?? camNormal).map((x) => x.toFixed(2))} rotated ${camRotated ? dir(camRotated).map((x) => x.toFixed(2)) : '-'} normal ${dir(camNormal).map((x) => x.toFixed(2))}`);
  await page.click('[data-testid=sketch-exit]');
  await page.waitForTimeout(300);
  const restored = !(await page.evaluate(() => window.__plainsolid.isOrtho()));
  check('leaving the sketch restores the projection', restored);

  // the point a click names is the server's: for a full circle, a point on the ring, not its centre
  const ringCheck = await page.evaluate(() => {
    const s = window.__plainsolid.getState();
    const h = s.mesh.header;
    const ring = Object.entries(h.edge_labels).map(([k]) => Number(k)).find((id) => { const [st, n] = h.edge_ranges[id]; const p = s.mesh.edgePositions; return n > 8 && Math.hypot(p[st * 3] - p[(st + n - 1) * 3], p[st * 3 + 1] - p[(st + n - 1) * 3 + 1], p[st * 3 + 2] - p[(st + n - 1) * 3 + 2]) < 1e-3; });
    if (ring === undefined) return null;
    const c = window.__plainsolid.actions.entityCenter({ kind: 'edge', id: ring });
    const [st, n] = h.edge_ranges[ring]; const p = s.mesh.edgePositions;
    let acc = [0, 0, 0]; for (let i = st; i < st + n; i++) { acc[0] += p[i * 3]; acc[1] += p[i * 3 + 1]; acc[2] += p[i * 3 + 2]; }
    const avg = acc.map((v) => v / n);
    let onCurve = Infinity; for (let i = st; i < st + n; i++) onCurve = Math.min(onCurve, Math.hypot(p[i * 3] - c[0], p[i * 3 + 1] - c[1], p[i * 3 + 2] - c[2]));
    return { ring, onCurve, fromAvg: Math.hypot(avg[0] - c[0], avg[1] - c[1], avg[2] - c[2]) };
  });
  check('the point a click names for a full circle lies on the ring, not at its centre', !!ringCheck && ringCheck.onCurve < 0.2 && ringCheck.fromAvg > 1, JSON.stringify(ringCheck));

  // an edge picks without a tool: the panel describes it, and fillet starts from it with the edge already picked
  await page.evaluate(() => window.__plainsolid.actions.openFeatureDialog(null));
  const edgeToPick = await page.evaluate(() => {
    const h = window.__plainsolid.getState().mesh.header;
    const e = Object.entries(h.edge_tags ?? {}).find(([, t]) => t.includes(':top') && t.includes('bottom'));
    return e ? Number(e[0]) : 0;
  });
  await page.evaluate((id) => window.__plainsolid.actions.pickEntity('edge', id), edgeToPick);
  await page.waitForSelector('[data-testid=edge-info]', { timeout: 5000 });
  const edgeInfo = (await page.locator('[data-testid=edge-info]').textContent()) ?? '';
  await page.click('[data-testid=edge-fillet]');
  await page.waitForSelector('[data-testid=feature-dialog]');
  await page.waitForTimeout(200);
  check('a clicked edge is selected, described, and a fillet started from it has the edge picked and the pick armed',
    (await st()).selectedEdge === edgeToPick && /edge\s*\d+ · \w+ · .* mm/.test(edgeInfo) && (await page.locator('.pick-chip').count()) === 1 && (await st()).pickRequest !== null, `${edgeInfo} · chips ${await page.locator('.pick-chip').count()} · armed ${(await st()).pickRequest !== null}`);
  await page.click('[data-testid=feature-cancel]');
  await page.waitForTimeout(200);

  // a body edge used in a relation from inside the holes sketch: converted silently, selected
  await page.click('[data-testid=feature-holes]');
  await page.click('.btn:has-text("edit sketch")');
  await page.waitForSelector('[data-testid=sketch-bar]');
  await page.waitForFunction(() => window.__plainsolid.getState().mesh?.header.upto === 'outer', null, { timeout: 15000 });
  await page.waitForTimeout(400);
  const edgeForRelation = await page.evaluate(() => {
    const h = window.__plainsolid.getState().mesh.header;
    const e = Object.entries(h.edge_tags ?? {}).find(([, t]) => t.includes(':top') && t.includes('bottom'));
    return e ? Number(e[0]) : null;
  });
  h = (await st()).hash;
  await page.evaluate((id) => window.__plainsolid.actions.useBodyInRelation('edge', id), edgeForRelation);
  await waitHash(h);
  const sRel = await st();
  check('shift+click on a body edge inside a sketch converts it as construction and selects it for a relation',
    /holes\.project\("edge1", body\.edges\.top\.from_sketch\("bottom"\)\)/.test(readFile()) && sRel.sketchMode?.selection?.includes('edge1'),
    `${readFile().match(/holes\.project\("edge1".*/)?.[0] ?? 'no statement'} · selection ${sRel.sketchMode?.selection?.join(',')}`);
  // a plain click on the body inside the sketch selects it for conversion; convert writes a project entity
  const faceForConvert = await page.evaluate(() => { const h = window.__plainsolid.getState().mesh.header; const e = Object.entries(h.face_labels).find(([, l]) => l === 'body'); return e ? Number(e[0]) : null; });
  await page.evaluate((id) => window.__plainsolid.actions.toggleBodySelect({ kind: 'face', id }, false), faceForConvert);
  await page.waitForSelector('[data-testid=body-selected]', { timeout: 5000 });
  h = (await st()).hash;
  await page.click('[data-testid=convert-body]');
  await waitHash(h);
  await page.waitForTimeout(300);
  const sConv = await st();
  const convName = sConv.status.match(/^converted (\w+)/)?.[1] ?? 'face?';  // an earlier check already made face1 with the convert tool
  check('a body face selected inside the sketch converts into its outline as sketch geometry',
    new RegExp(`holes\\.project\\("${convName}", body\\.faces\\.[^)]*\\)?, construction=False\\)`).test(readFile()) && sConv.sketchMode?.selection?.includes(convName) && sConv.sketchMode?.bodySelection?.length === 0,
    `${readFile().match(new RegExp(`holes\\.project\\("${convName}".*`))?.[0] ?? 'no statement'} · selection ${JSON.stringify(sConv.sketchMode?.selection)}`);
  await page.click('[data-testid=sketch-exit]');
  await page.waitForTimeout(300);
}

// ---- assemblies: the node reference assembly with instances and mates ----------------------
{
  const NODE = path.join(PROJ, 'node.py');
  const readNode = () => fs.readFileSync(NODE, 'utf8');
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  const waitHash = async (h) => page.waitForFunction((h) => { const s = window.__plainsolid.getState(); return s.hash !== h && s.tree?.hash === s.hash; }, h, { timeout: 30000 }).then(() => page.waitForTimeout(700));
  // the checks above edited bracket.py, which the assembly instances: put the original back
  // (the watcher refreshes the assembly through its dependency list) before looking at it
  fs.writeFileSync(path.join(PROJ, 'bracket.py'), fs.readFileSync(new globalThis.URL('../zoo/bracket.py', import.meta.url), 'utf8'));
  await page.click('[data-testid="doc-tab-node"]');
  await page.waitForSelector('[data-testid=feature-panel_mid]', { timeout: 30000 });
  await page.waitForFunction(() => { const s = window.__plainsolid.getState(); return s.mesh?.header.items?.length === 8 && s.tree?.evaluation.assembly?.dof === 3; }, null, { timeout: 60000 });
  await page.waitForTimeout(500);
  const rows = await page.locator('.tree-row').count();
  const summary = (await page.locator('[data-testid=assembly-status]').textContent()) ?? '';
  await page.click('[data-testid=assembly-status-info]');
  const pop = (await page.locator('[data-testid=assembly-status-pop]').textContent()) ?? '';
  await page.click('[data-testid=assembly-status-info]');
  check('the assembly tree lists instances and mates with a status chip and its details on demand',
    rows === 28 && /3 free/.test(summary) && /gland1, gland2, antenna/.test(pop) && (await page.locator('[data-testid=mode-chip]').textContent()) === 'assembly', `rows ${rows} · ${summary.trim()} · ${pop.slice(0, 60)}`);
  await page.screenshot({ path: 'screenshot-node.png' });

  // an instance in the tree: its panel shows the file and how the mates hold it
  await page.click('[data-testid=feature-lid]');
  await page.waitForSelector('[data-testid=instance-props]');
  const lidProps = (await page.locator('[data-testid=instance-props]').textContent()) ?? '';
  check('an instance panel shows its file and constraint status', lidProps.includes('lid.py') && lidProps.includes('held by its mates'), lidProps.slice(0, 80));

  // a click on the model selects the instance it belongs to
  const panelFace = await page.evaluate(() => { const it = window.__plainsolid.getState().mesh.header.items.find((i) => i.path === 'panel'); return it ? it.faces[0] : null; });
  await page.evaluate((id) => window.__plainsolid.actions.selectFace(id), panelFace);
  await page.waitForTimeout(200);
  check('a face of an instance selects that instance', (await st()).selected === 'panel', `selected ${(await st()).selected}`);

  // click-to-selector on instances: semantic through the part's identity map, geometric on vendor STEP
  const exprs = await page.evaluate(() => {
    const p = window.__plainsolid; const h = p.getState().mesh.header;
    const lid = h.items.find((i) => i.path === 'lid'); const gland = h.items.find((i) => i.path === 'gland1');
    let lidTop = null;
    for (let f = lid.faces[0]; f < lid.faces[0] + lid.faces[1]; f++) if (h.face_labels[String(f)] === 'plate' && (h.face_tags[String(f)] ?? []).includes(':top')) { lidTop = f; break; }
    return { lid: p.actions.selectorFor('face', lidTop), gland: p.actions.selectorFor('face', gland.faces[0]) };
  });
  check('instance faces get semantic selectors from the part, vendor faces nearest ones',
    exprs.lid === 'lid.faces.of("plate").top' && /^gland1\.faces\.nearest\(\(/.test(exprs.gland ?? ''), JSON.stringify(exprs));

  // the assembly panel: bill of materials and the interference check
  await page.evaluate(() => window.__plainsolid.actions.select(null));
  await page.waitForSelector('[data-testid=bom-table]', { timeout: 30000 });
  const bomRows = await page.locator('[data-testid=bom-table] tr').count();
  const bomTotal = (await page.locator('[data-testid=bom-total]').textContent()) ?? '';
  check('the bill of materials lists one row per part with the total mass', bomRows === 7 && /8 instances · \d/.test(bomTotal), `${bomRows} rows · ${bomTotal}`);
  await page.click('[data-testid=check-interference]');
  await page.waitForSelector('[data-testid=interference-result]', { timeout: 30000 });
  check('the interference check finds none in the reference assembly', ((await page.locator('[data-testid=interference-result]').textContent()) ?? '').includes('no interference'));

  // + instance from the dialog: named after the file, at the origin
  let h = (await st()).hash;
  await page.click('[data-testid=new-instance]');
  await page.fill('[data-testid=instance-path]', 'bracket.py');
  await page.click('[data-testid=feature-ok]');
  await waitHash(h);
  check('the instance dialog adds an instance named after its file', readNode().includes('bracket1 = instance("bracket1", "bracket.py")'), readNode().match(/bracket1.*/)?.[0] ?? 'no statement');
  await page.locator('[data-testid=viewport] canvas').focus();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+z' : 'Control+z');
  await page.waitForFunction(() => !window.__plainsolid.getState().tree?.features.some((f) => f.name === 'bracket1'), null, { timeout: 30000 });
  await page.waitForTimeout(400);

  // + mate from the dialog, with plane references chosen from the selects: written, solved, and its redundancy reported
  h = (await st()).hash;
  await page.click('[data-testid=new-mate]');
  await page.selectOption('[data-testid=mate-kind]', 'distance');
  await page.selectOption('[data-testid=mate-a-select]', 'lid.planes.XY');
  await page.selectOption('[data-testid=mate-b-select]', 'box.planes.XY');
  await page.fill('[data-testid=mate-value]', '-40');
  await page.click('[data-testid=feature-ok]');
  await waitHash(h);
  await page.waitForFunction(() => window.__plainsolid.getState().tree?.features.some((f) => f.name === 'distance1'), null, { timeout: 30000 });
  await page.waitForTimeout(300);
  const mateLine = readNode().match(/distance\("distance1".*/)?.[0] ?? 'no statement';
  const mateRow = await page.locator('[data-testid=feature-distance1] .badge.warn').count();
  check('the mate dialog writes a mate; a redundant one is flagged in the tree', mateLine === 'distance("distance1", lid.planes.XY, box.planes.XY, -40)' && mateRow === 1, `${mateLine} · warn badges ${mateRow}`);
  const s3 = await st();
  check('the assembly stays solved after the extra mate', s3.tree.evaluation.assembly.conflicting.length === 0 && s3.tree.evaluation.assembly.redundant.includes('distance1'));
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+z' : 'Control+z');
  await page.waitForFunction(() => !window.__plainsolid.getState().tree?.features.some((f) => f.name === 'distance1'), null, { timeout: 30000 });
  await page.waitForTimeout(400);

  // a mate value edit re-solves and writes the pose back into the instance statement
  await page.click('[data-testid=feature-antenna_up]');
  await page.waitForSelector('[data-testid=mate-props]');
  h = (await st()).hash;
  const val = page.locator('[data-testid=mate-props] .field input').first();
  await val.fill('25'); await val.press('Enter');
  await waitHash(h);
  check('editing a mate value moves the instance and writes its pose back',
    readNode().includes('antenna = instance("antenna", "vendor/antenna.step", material="abs", at=(60, 0, 25), rotate=(0, 90, 0))'),
    readNode().match(/antenna = instance.*/)?.[0] ?? '');
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+z' : 'Control+z');
  await page.waitForTimeout(1500);

  // the move tool: a drag turns the gland about its free axis and writes the pose; the lid does not move
  const faceOf = async (name) => { const items = (await st()).mesh.header.items; const it = items.find((x) => x.path === name || x.path.startsWith(`${name}.`)); return it ? it.faces[0] : null; };
  await page.evaluate(() => window.__plainsolid.actions.setTool('move'));
  await page.evaluate(() => window.__plainsolid.actions.setMoveMode('rotate'));
  await page.waitForSelector('[data-testid=move-panel]');
  // a horizontal drag turns about the screen's up axis: look from the top so that axis is the gland's (world Y),
  // whatever camera the tab has (each document keeps its own now)
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+5' : 'Control+5');
  await page.waitForTimeout(400);
  const glandFace = await faceOf('gland1');
  const took = await page.evaluate((id) => window.__plainsolid.actions.beginInstanceDrag({ kind: 'face', id }), glandFace);
  await page.evaluate(() => window.__plainsolid.actions.dragInstance(60, 0));
  await page.waitForFunction(() => !!window.__plainsolid.getState().posePreview, null, { timeout: 15000 }).catch(() => {});
  const previewed = !!(await st()).posePreview;
  h = (await st()).hash;
  await page.evaluate(() => window.__plainsolid.actions.endInstanceDrag());
  await waitHash(h);
  await page.waitForTimeout(300);
  const glandLine = readNode().match(/gland1 = instance.*/)?.[0] ?? '';
  const spun = Number(glandLine.match(/rotate=\(-90, 0, ([-\d.]+)\)/)?.[1] ?? 0);
  check('the move tool turns the gland about its free axis, previews it, and writes the pose on release',
    took && previewed && Math.abs(spun) > 5 && glandLine.includes('at=(-30, 40, 20)') && (await st()).posePreview === null, glandLine);
  await page.evaluate(() => window.__plainsolid.actions.setMoveMode('translate'));
  const lidFace = await faceOf('lid');
  await page.evaluate((id) => window.__plainsolid.actions.beginInstanceDrag({ kind: 'face', id }), lidFace);
  await page.evaluate(() => window.__plainsolid.actions.dragInstance(60, 0));
  await page.waitForFunction(() => window.__plainsolid.getState().status.includes('fully constrained'), null, { timeout: 15000 }).catch(() => {});
  const lidStatus = (await st()).status;
  await page.evaluate(() => window.__plainsolid.actions.endInstanceDrag());
  await page.waitForTimeout(400);
  check('a fully constrained part does not move and the status says why', lidStatus.includes('fully constrained') && readNode().includes('lid = instance("lid", "lid.py", color="#d8d8d0", at=(0, 0, 40))'), lidStatus);
  await page.evaluate(() => window.__plainsolid.actions.setTool('move'));
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+z' : 'Control+z');
  await page.waitForFunction(() => /gland1 = instance\("gland1", "vendor\/gland_m12.step", material="nylon", at=\(-30, 40, 20\), rotate=\(-90, 0, 0\)\)/.test(window.__plainsolid.getState().source), null, { timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(400);
  // the mate flow: a selected face is the first reference, the second pick is armed at once, both stay lit,
  // the parts preview the mate, and cancel puts them back
  await page.evaluate((id) => window.__plainsolid.actions.pickEntity('face', id), lidFace);
  await page.waitForTimeout(200);
  await page.click('[data-testid=new-mate]');
  await page.waitForSelector('[data-testid=mate-a]');
  await page.waitForTimeout(200);
  const aText = await page.locator('[data-testid=mate-a] .pick-value').textContent();
  const armedB = !!(await st()).pickRequest && (await st()).pickRequest.hint.includes('reference b');
  const boxFace = await faceOf('box');
  await page.evaluate((id) => window.__plainsolid.actions.pickEntity('face', id), boxFace);
  await page.waitForSelector('[data-testid=mate-preview]', { timeout: 15000 });
  await page.waitForTimeout(300);
  const sm = await st();
  check('+ mate starts from the selected face, arms the second pick, keeps both lit and previews the mate',
    aText !== 'nothing picked' && armedB && sm.dialogPicks.length === 2 && !!(await page.locator('[data-testid=mate-preview]').textContent()) && (sm.posePreview !== null || (await page.locator('[data-testid=mate-preview]').textContent()).length > 0),
    `a: ${aText} · ${await page.locator('[data-testid=mate-preview]').textContent()}`);
  h = (await st()).hash;
  await page.click('[data-testid=feature-cancel]');
  await page.waitForTimeout(300);
  check('cancel closes the mate dialog, clears the preview and writes nothing', (await st()).posePreview === null && (await st()).hash === h && (await page.locator('[data-testid=mate-a]').count()) === 0);

  // open documents are tabs; a STEP viewer becomes an assembly, then a body becomes an editable part
  await page.click('[data-testid="doc-tab-node_review"]');
  await page.waitForSelector('[data-testid="instance-node.glands.gland_1"]', { timeout: 30000 });
  check('open documents are tabs and clicking one switches to it', ((await page.locator('.doc-tab.active').textContent()) ?? '').includes('node_review')
    && (await page.locator('[data-testid=mode-chip]').textContent()) === 'STEP viewer');
  await page.click('[data-testid=make-editable-viewer]');
  await page.click('[data-testid=make-editable-ok]');
  await page.waitForSelector('[data-testid=feature-gland_2]', { timeout: 30000 });
  await page.waitForTimeout(300);
  const exploded = fs.readFileSync(path.join(PROJ, 'node_review.py'), 'utf8');
  check('make editable turns the viewer into an assembly of the file\'s bodies, grouped by their path',
    (await page.locator('[data-testid=mode-chip]').textContent()) === 'assembly' && /gland_1 = instance\("gland_1", "vendor\/node_stub.step#node.glands.gland_1", at=\(-15, 20, 0\), rotate=\(90, 0, 0\)\)/.test(exploded)
      && (await page.locator('.tree-group').count()) === 2 && !fs.existsSync(path.join(PROJ, 'gland_1.py')),
    exploded.split('\n').filter((l) => l.includes('instance(')).length + ' instances');
  // the grey group rows of an editable assembly open their node of the file the same way
  await page.click('[data-testid="group-node.glands"]', { button: 'right' });
  await page.waitForSelector('[data-testid=context-menu]');
  const groupMenu = (await st()).contextMenu?.entries.filter((e) => !e.sep).map((e) => e.label) ?? [];
  await page.click('[data-testid=ctx-open-sub-assembly]');
  await page.waitForSelector('[data-testid="doc-tab-glands"]', { timeout: 30000 });
  await page.waitForSelector('[data-testid="instance-glands.gland_2"]', { timeout: 30000 });
  check('a group row of an editable assembly offers visibility and opens its sub-assembly in a tab', ['isolate', 'hide', 'transparent', 'open sub-assembly'].every((l) => groupMenu.includes(l))
    && ((await page.locator('.doc-tab.active').textContent()) ?? '').includes('glands'), groupMenu.join(' | '));
  await page.click('.doc-tab.active .doc-tab-close');
  await page.waitForTimeout(500);
  await page.click('[data-testid="doc-tab-node_review"]');
  await page.waitForSelector('[data-testid=feature-gland_1]', { timeout: 30000 });
  await page.click('[data-testid=feature-gland_1]', { button: 'right' });
  await page.waitForSelector('[data-testid=context-menu]');
  const bodyMenu = (await st()).contextMenu?.entries.filter((e) => !e.sep).map((e) => e.label) ?? [];
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  check('an instance that came out of a sub-assembly offers to open that sub-assembly', bodyMenu.includes('open glands') && !bodyMenu.includes('open node'), bodyMenu.join(' | '));
  await page.click('[data-testid=feature-gland_1]');
  await page.click('[data-testid=edit-part]');
  await page.waitForSelector('[data-testid="doc-tab-gland_1"]', { timeout: 30000 });
  await page.waitForFunction(() => { const s = window.__plainsolid.getState(); return s.tree?.kind === 'part' && s.tree.features.some((f) => f.name === 'body'); }, null, { timeout: 30000 });
  const review = fs.readFileSync(path.join(PROJ, 'node_review.py'), 'utf8');
  check('edit part gives a body a part file, repoints its twin and opens the part in a tab',
    fs.existsSync(path.join(PROJ, 'gland_1.py')) && /gland_1 = instance\("gland_1", "gland_1.py", at=\(-15, 20, 0\), rotate=\(90, 0, 0\)\)/.test(review)
      && /gland_2 = instance\("gland_2", "gland_1.py"/.test(review) && /box = instance\("box", "vendor\/node_stub.step#node.box"\)/.test(review),
    review.split('\n').filter((l) => l.includes('instance(')).join(' | '));
  // export the posed node assembly to STEP from the toolbar action
  await page.click('[data-testid="doc-tab-node"]');
  await page.waitForFunction(() => window.__plainsolid.getState().tree?.meta?.name === 'node', null, { timeout: 30000 });
  const exported = await page.evaluate(() => window.__plainsolid.actions.exportDocument('step', 'out/node_export.step'));
  check('export STEP writes the posed assembly next to the document', !!exported && fs.existsSync(path.join(PROJ, 'out', 'node_export.step')) && fs.statSync(path.join(PROJ, 'out', 'node_export.step')).size > 100000,
    exported ?? 'no path');
  await page.click('[data-testid="doc-tab-node_review"]');
  await page.waitForSelector('[data-testid=feature-gland_2]', { timeout: 30000 });
  await page.waitForTimeout(300);
  const groups = await page.locator('.tree-group').allTextContents();
  check('the exploded import shows its remaining STEP bodies grouped by their path in the file',
    (await page.locator('[data-testid^="feature-"]').count()) === 4 && groups.length === 1 && groups[0].includes('node'), `${groups.length} groups: ${groups.join(',')}`);
}

// ---- drawings: the bracket drawing as a sheet ----------------------------------------------
{
  const st = () => page.evaluate(() => window.__plainsolid.getState());
  const waitHash = (h) => page.waitForFunction((x) => window.__plainsolid.getState().hash !== x && !window.__plainsolid.getState().loading, h, { timeout: 30000 });
  const readDwg = () => fs.readFileSync(path.join(PROJ, 'bracket_dwg.py'), 'utf8');
  await page.click('[data-testid=open-menu]');
  await page.fill('[data-testid=open-path]', 'bracket_dwg.py');
  await page.press('[data-testid=open-path]', 'Enter');
  await page.waitForSelector('[data-testid=dwg-view-section]', { timeout: 30000 });
  await page.waitForTimeout(400);
  const s0 = await st();
  check('the drawing opens as a sheet with its views, dimensions and note',
    (await page.locator('[data-testid=mode-chip]').textContent()) === 'drawing' && (await page.locator('[data-testid^=dwg-view-]').count()) === 5
      && (await page.locator('[data-testid^=dwg-dim-text-]').count()) === 9 && (await page.locator('[data-testid^=dwg-note-]').count()) === 1
      && s0.tree.evaluation.drawing.views[4].label === 'SECTION A-A',
    `${await page.locator('[data-testid^=dwg-view-]').count()} views`);
  await page.screenshot({ path: 'shot-drawing.png' });
  // the sheet's paper maps sheet mm to the screen
  const paper = await page.locator('.sheet-paper').boundingBox();
  const W = s0.tree.evaluation.drawing.sheet.width, H = s0.tree.evaluation.drawing.sheet.height;
  const toScreen = (x, y) => [paper.x + (x / W) * paper.width, paper.y + (1 - y / H) * paper.height];
  // click a view: selected in the tree, its panel opens
  const isoBox = await page.locator('[data-testid=dwg-view-iso] .dwg-view-hit').boundingBox();
  const [ix, iy] = [isoBox.x + isoBox.width / 2, isoBox.y + isoBox.height / 2];
  await page.mouse.click(ix, iy);
  await page.waitForTimeout(200);
  check('clicking a view on the sheet selects it and opens its panel', (await st()).selected === 'iso' && (await page.locator('[data-testid=view-props]').count()) === 1);
  // drag the iso view: its place is written into the file
  let h = (await st()).hash;
  await page.mouse.move(ix, iy); await page.mouse.down();
  await page.mouse.move(ix + 20, iy, { steps: 4 }); await page.mouse.move(ix + 40, iy, { steps: 4 });
  await page.mouse.up();
  await waitHash(h);
  await page.waitForTimeout(300);
  const isoLine = readDwg().match(/iso = view.*/)?.[0] ?? '';
  const isoX = Number(isoLine.match(/at=\(([\d.]+),/)?.[1] ?? 0);
  check('dragging a view writes its new place into the file', isoX > 245 && isoX < 275 && isoLine.includes('scale=0.75, hidden=False'), isoLine);
  // the dimension tool: two edges of the front view, then a click below it
  await page.click('[data-testid=new-dimension]');
  await page.waitForSelector('[data-testid=sheet-hint]');
  // straight edges have a zero-width box, so Playwright's click refuses them: click their centre
  for (const sel of ['[data-testid=dwg-pick-front][data-ref*=\'from_sketch("outer_wall")\']', '[data-testid=dwg-pick-front][data-ref*=\'from_sketch("right")\']']) {
    const b = await page.locator(sel).first().boundingBox();
    await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
    await page.waitForTimeout(150);
  }
  await page.waitForTimeout(200);
  const picks = (await st()).drawingPicks;
  h = (await st()).hash;
  const [px, py] = toScreen(65, 100);
  await page.mouse.click(px, py);
  await waitHash(h);
  await page.waitForTimeout(300);
  const s1 = await st();
  const dim1 = s1.tree.evaluation.drawing.dimensions.find((d) => d.name === 'dim1');
  check('the dimension tool picks two labelled edges and writes a distance dimension where the sheet was clicked',
    picks.length === 2 && /dimension\("dim1", front\.edges\.of\("body"\)\.\w+\.from_sketch\("outer_wall"\), front\.edges\.of\("body"\)\.\w+\.from_sketch\("right"\), at=\(-?0(\.\d)?, -(39|40)(\.\d)?\)\)/.test(readDwg())
      && dim1 && dim1.value === 60 && s1.drawingTool === 'dimension',
    readDwg().match(/dimension\("dim1".*/)?.[0] ?? `no statement · ${picks.map((p) => p.ref).join(' | ')}`);
  await page.keyboard.press('Escape');
  // a note where the sheet is clicked (the dialog stub answers "XZ")
  await page.click('[data-testid=new-note]');
  h = (await st()).hash;
  const [nx, ny] = toScreen(120, 30);
  await page.mouse.click(nx, ny);
  await waitHash(h);
  await page.waitForTimeout(300);
  check('the note tool writes a note at the clicked point', /note\("note1", "XZ", at=\(1(19|20)(\.\d)?, (29|30)(\.\d)?\)\)/.test(readDwg()), readDwg().match(/note\("note1".*/)?.[0] ?? 'no statement');
  // the sheet size and the title block fields from the panel with nothing selected
  await page.evaluate(() => window.__plainsolid.actions.select(null));
  await page.waitForSelector('[data-testid=meta-sheet]');
  h = (await st()).hash;
  await page.selectOption('[data-testid=meta-sheet]', 'A3');
  await waitHash(h);
  h = (await st()).hash;
  await page.fill('[data-testid=meta-revision]', 'B'); await page.press('[data-testid=meta-revision]', 'Enter');
  await waitHash(h);
  await page.waitForTimeout(300);
  const s2 = await st();
  check('the sheet size and revision are set from the panel and rewrite the meta line',
    /meta\(kind="drawing", name="bracket_dwg", of="bracket.py", sheet="A3", revision="B", author="Paolo"\)/.test(readDwg()) && s2.tree.evaluation.drawing.sheet.width === 420,
    readDwg().match(/meta\(.*/)?.[0] ?? '');
  // exports next to the drawing
  const pdf = await page.evaluate(() => window.__plainsolid.actions.exportDocument('pdf', 'out/bracket_dwg.pdf'));
  const dxf = await page.evaluate(() => window.__plainsolid.actions.exportDocument('dxf', 'out/bracket_dwg.dxf'));
  check('the drawing exports to PDF and DXF', !!pdf && !!dxf && fs.statSync(path.join(PROJ, 'out', 'bracket_dwg.pdf')).size > 5000 && fs.statSync(path.join(PROJ, 'out', 'bracket_dwg.dxf')).size > 20000);
  // editing the part in its own tab reaches the drawing
  await page.click('[data-testid="doc-tab-bracket"]');
  await page.waitForFunction(() => window.__plainsolid.getState().tree?.kind === 'part', null, { timeout: 30000 });
  h = (await st()).hash;
  await page.evaluate(() => window.__plainsolid.actions.edit({ op: 'set_parameter', name: 'width', value: 70 }));
  await waitHash(h);
  await page.click('[data-testid="doc-tab-bracket_dwg"]');
  await page.waitForFunction(() => window.__plainsolid.getState().tree?.evaluation.drawing?.dimensions.find((d) => d.name === 'width')?.text === '70', null, { timeout: 30000 }).catch(() => {});
  const widthText = (await st()).tree.evaluation.drawing.dimensions.find((d) => d.name === 'width').text;
  check('a parameter edit in the part tab updates the drawing\'s dimensions', widthText === '70', `width reads ${widthText}`);
  await page.click('[data-testid="doc-tab-bracket"]');
  await page.waitForFunction(() => window.__plainsolid.getState().tree?.kind === 'part', null, { timeout: 30000 });
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+z' : 'Control+z');
  await page.waitForTimeout(1200);
}

// the layout at common laptop sizes, the side panels' splitters and collapse toggles
{
  const fits = async () => page.evaluate(() => {
    const q = (sel) => document.querySelector(sel);
    const left = q('.left'), right = q('.right'), vp = q('.viewport') ?? q('.sheet-host');
    return { page: document.documentElement.scrollWidth <= window.innerWidth, toolbar: q('.toolbar').scrollHeight <= 40,
             left: left.scrollWidth <= left.clientWidth + 1, right: right.scrollWidth <= right.clientWidth + 1, viewport: vp.clientWidth };
  });
  const widths = async () => page.evaluate(() => [document.querySelector('.left').getBoundingClientRect().width, document.querySelector('.right').getBoundingClientRect().width]);
  for (const [w, h] of [[1280, 800], [1366, 768], [1536, 864], [1920, 1080]]) {
    await page.setViewportSize({ width: w, height: h });
    await page.waitForTimeout(300);
    const f = await fits();
    check(`the layout fits ${w}x${h}: no horizontal scrolling, viewport ${f.viewport}px wide`, f.page && f.toolbar && f.left && f.right && f.viewport >= 420, JSON.stringify(f));
  }
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.waitForTimeout(300);
  // the right panel at its narrowest still shows everything without a horizontal scrollbar
  const rs = await page.locator('[data-testid=right-splitter]').boundingBox();
  await page.mouse.move(rs.x + 2, rs.y + 100); await page.mouse.down(); await page.mouse.move(rs.x + 400, rs.y + 100, { steps: 8 }); await page.mouse.up();
  await page.waitForTimeout(200);
  let [leftW, rightW] = await widths();
  const narrow = await fits();
  check('the right panel stops at its minimum width and nothing overflows it', rightW <= 230 && narrow.right, `${rightW}px ${JSON.stringify(narrow)}`);
  await page.screenshot({ path: 'shot-narrow-panel.png' });
  // the tree's splitter widens it
  const ls = await page.locator('[data-testid=left-splitter]').boundingBox();
  await page.mouse.move(ls.x + 2, ls.y + 100); await page.mouse.down(); await page.mouse.move(ls.x + 82, ls.y + 100, { steps: 8 }); await page.mouse.up();
  await page.waitForTimeout(200);
  [leftW, rightW] = await widths();
  check('dragging the tree splitter widens the tree', leftW >= 320 && leftW <= 340, `${leftW}px`);
  // collapsing both panels gives the viewport the width; the toggles bring them back at the remembered widths
  await page.click('[data-testid=left-toggle]'); await page.click('[data-testid=right-toggle]');
  await page.waitForTimeout(300);
  const collapsed = await fits();
  check('collapsing both side panels gives the viewport almost the window', collapsed.viewport >= 1280 - 60 && collapsed.page, `${collapsed.viewport}px`);
  await page.screenshot({ path: 'shot-collapsed.png' });
  await page.click('[data-testid=left-toggle]'); await page.click('[data-testid=right-toggle]');
  await page.waitForTimeout(300);
  const back = await widths();
  const stored = await page.evaluate(() => [localStorage.getItem('plainsolid.leftWidth'), localStorage.getItem('plainsolid.rightWidth'), localStorage.getItem('plainsolid.leftOpen')]);
  check('the panels come back at their remembered widths', Math.abs(back[0] - leftW) < 1 && Math.abs(back[1] - rightW) < 1 && Number(stored[0]) === Math.round(leftW) && Number(stored[1]) === Math.round(rightW) && stored[2] === '1', JSON.stringify([back, stored]));
  await page.screenshot({ path: 'shot-laptop.png' });
  await page.evaluate(() => { localStorage.removeItem('plainsolid.leftWidth'); localStorage.removeItem('plainsolid.rightWidth'); });
  await page.setViewportSize({ width: 1400, height: 900 });
}

// `plainsolid open` with no tab open: the page URL names the files. A project file opens; a STEP
// file from outside the project goes through the import dialog, which copies it in and opens the copy.
{
  const outside = path.resolve(PROJ, '..', `outside-${process.pid}.step`);
  fs.copyFileSync(path.join(PROJ, 'vendor', 'node_stub.step'), outside);
  await page.goto(`${URL}?open=lid.py&import=${encodeURIComponent(outside)}`);
  await page.waitForSelector('[data-testid=doc-tab-lid]', { timeout: 20000 });
  await page.waitForSelector('[data-testid=import-pop]', { timeout: 20000 });
  check('the URL query opens the project file and offers the outside STEP file for import',
        (await page.locator('[data-testid=import-name]').inputValue()) === `outside-${process.pid}` && !page.url().includes('?'));
  await page.fill('[data-testid=import-folder]', 'proposals');
  await page.fill('[data-testid=import-name]', 'node_v4');
  await page.click('[data-testid=import-go]');
  await page.waitForSelector('[data-testid=doc-tab-node_v4]', { timeout: 30000 });
  await page.waitForFunction(() => !document.querySelector('[data-testid=import-pop]'), null, { timeout: 5000 });
  check('the import copies the file into proposals/ and opens the copy',
        fs.existsSync(path.join(PROJ, 'proposals', 'node_v4.step')) && fs.existsSync(path.join(PROJ, 'proposals', 'node_v4.py')) && fs.existsSync(outside));
  fs.unlinkSync(outside);
  await page.click('[data-testid=file-menu]');
  check('the file menu offers to quit the server', (await page.locator('[data-testid=quit-server]').count()) === 1);
  await page.keyboard.press('Escape');
}

check('no console or page errors', errors.length === 0, errors.slice(0, 3).join(' | '));
await browser.close();
const failed = results.filter(([, ok]) => !ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
