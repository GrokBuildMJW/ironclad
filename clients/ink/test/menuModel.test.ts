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

test('menuKey — Enter / Backspace / plain key fall through as none', () => {
  assert.equal(menuKey(0, res, emptyKey({return: true})).type, 'none');
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
