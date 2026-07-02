import test from 'node:test';
import assert from 'node:assert/strict';
import {emptyKey} from '../src/render/hooks.js';
import {completions, COMMANDS, type Command} from '../src/commands.js';
import {menuKey, clampSel, menuWindow, completionText, MENU_MAX_VISIBLE} from '../src/ui/menuModel.js';

const res = completions('res'); // [reset, resume] (note: 're' would also match 'read')

test('menuKey — closed menu (no items) → always none', () => {
  assert.equal(menuKey(0, [], emptyKey({tab: true})).type, 'none');
  assert.equal(menuKey(0, [], emptyKey({downArrow: true})).type, 'none');
  assert.equal(menuKey(0, [], emptyKey({escape: true})).type, 'none');
});

test('menuKey — ↑/↓ move the selection and wrap', () => {
  assert.deepEqual(menuKey(0, res, emptyKey({downArrow: true})), {type: 'move', sel: 1});
  assert.deepEqual(menuKey(1, res, emptyKey({downArrow: true})), {type: 'move', sel: 0}); // wrap to top
  assert.deepEqual(menuKey(0, res, emptyKey({upArrow: true})), {type: 'move', sel: 1}); // wrap to bottom
  assert.deepEqual(menuKey(1, res, emptyKey({upArrow: true})), {type: 'move', sel: 0});
});

test('menuKey — Tab completes the highlighted command', () => {
  const a = menuKey(1, res, emptyKey({tab: true}));
  assert.equal(a.type, 'complete');
  assert.equal(a.type === 'complete' && a.cmd.name, 'resume');
});

test('menuKey — Esc closes', () => {
  assert.deepEqual(menuKey(0, res, emptyKey({escape: true})), {type: 'close'});
});

test('menuKey — Enter accepts the highlighted command (unless the buffer already is it)', () => {
  // buffer still a prefix → Enter fills the highlighted command (↓-then-Enter)
  const a = menuKey(1, res, emptyKey({return: true}), '/res');
  assert.equal(a.type, 'complete');
  assert.equal(a.type === 'complete' && a.cmd.name, 'resume');
  // buffer already equals the completed command → fall through (none) so the line submits
  assert.equal(menuKey(1, res, emptyKey({return: true}), completionText(res[1]!)).type, 'none');
});

test('menuKey — single match accepts via Enter (#17: no ↓ feedback for one item)', () => {
  const one = [res[0]!];
  assert.equal(menuKey(0, one, emptyKey({return: true}), '/r').type, 'complete');
});

test('menuKey — single match accepts via ↓ or ↑ (#53: a lone value is picked by an arrow)', () => {
  const one = [res[0]!];
  const down = menuKey(0, one, emptyKey({downArrow: true}));
  const up = menuKey(0, one, emptyKey({upArrow: true}));
  assert.equal(down.type, 'complete');
  assert.equal(down.type === 'complete' && down.cmd.name, res[0]!.name);
  assert.equal(up.type, 'complete');
  // multiple matches still navigate (no premature accept)
  assert.deepEqual(menuKey(0, res, emptyKey({downArrow: true})), {type: 'move', sel: 1});
});

test('menuKey — Backspace / plain key fall through as none', () => {
  assert.equal(menuKey(0, res, emptyKey({backspace: true})).type, 'none');
  assert.equal(menuKey(0, res, emptyKey()).type, 'none'); // plain character
});

test('clampSel keeps the index in range', () => {
  assert.equal(clampSel(-3, 5), 0);
  assert.equal(clampSel(9, 5), 4);
  assert.equal(clampSel(2, 5), 2);
  assert.equal(clampSel(0, 0), 0); // empty
});

test('completionText — trailing space only when the command takes an argument', () => {
  const read = COMMANDS.find((c) => c.name === 'read') as Command; // usage <path>
  const help = COMMANDS.find((c) => c.name === 'help') as Command; // no usage
  assert.equal(completionText(read), '/read ');
  assert.equal(completionText(help), '/help');
});

test('#937 completionText — an argument completion inserts the token into the line', () => {
  const gate: Command = {name: 'gate', scope: 'server', desc: '', arg: true};
  const slug: Command = {name: '--slug', scope: 'server', desc: '', arg: true};
  const tests: Command = {name: 'tests', scope: 'server', desc: '', arg: true};
  assert.equal(completionText(gate, '/lifecycle '), '/lifecycle gate ');           // fill the first arg after a space
  assert.equal(completionText(slug, '/lifecycle gate --sl'), '/lifecycle gate --slug ');  // replace the partial flag
  assert.equal(completionText(tests, '/lifecycle gate --stages '), '/lifecycle gate --stages tests '); // fill a choice
});

test('menuWindow — short list returned whole; long list windows around sel', () => {
  assert.deepEqual(menuWindow(res, 0), {slice: [...res], offset: 0});
  const all = completions(''); // every command — longer than MENU_MAX_VISIBLE
  assert.ok(all.length > MENU_MAX_VISIBLE, 'fixture precondition');
  const top = menuWindow(all, 0);
  assert.equal(top.slice.length, MENU_MAX_VISIBLE);
  assert.equal(top.offset, 0); // selection at the top → window starts at 0
  const bottom = menuWindow(all, all.length - 1);
  assert.equal(bottom.slice.length, MENU_MAX_VISIBLE);
  assert.equal(bottom.offset, all.length - MENU_MAX_VISIBLE); // clamped to the end
  assert.equal(bottom.slice.at(-1), all.at(-1)); // last item visible
});
