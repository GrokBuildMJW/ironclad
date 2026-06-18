import test from 'node:test';
import assert from 'node:assert/strict';
import {Selection} from '../src/render/selection.js';
import {Surface} from '../src/render/surface.js';
import {Palette} from '../src/render/palette.js';

function grid(rows: string[]): Surface {
  const w = Math.max(...rows.map((r) => r.length));
  const s = new Surface(w, rows.length);
  rows.forEach((r, y) => s.setText(0, y, r));
  return s;
}

test('begin/extend builds a normalized range; hasSelection', () => {
  const sel = new Selection();
  assert.equal(sel.hasSelection, false);
  sel.begin(5, 2);
  sel.extend(1, 0); // dragged up-left → range must normalize
  assert.equal(sel.hasSelection, true);
  assert.deepEqual(sel.range, {start: {x: 1, y: 0}, end: {x: 5, y: 2}});
});

test('isSelected covers partial first/last rows and full middle rows', () => {
  const sel = new Selection();
  sel.begin(2, 0);
  sel.extend(3, 2);
  assert.equal(sel.isSelected(2, 0), true);
  assert.equal(sel.isSelected(1, 0), false, 'before start on first row');
  assert.equal(sel.isSelected(99, 1), true, 'full middle row');
  assert.equal(sel.isSelected(3, 2), true);
  assert.equal(sel.isSelected(4, 2), false, 'after end on last row');
});

test('extractText joins rows with newlines and trims trailing spaces', () => {
  const s = grid(['hello', 'world']);
  const sel = new Selection();
  sel.begin(0, 0);
  sel.extend(4, 1);
  assert.equal(sel.extractText(s), 'hello\nworld');
});

test('extractText excludes NoSelect (chrome) cells', () => {
  const s = grid(['[ab]']); // imagine [ ] are border chrome
  const noSelect = new Uint8Array(s.width * s.height);
  noSelect[0] = 1; // '['
  noSelect[3] = 1; // ']'
  const sel = new Selection();
  sel.begin(0, 0);
  sel.extend(3, 0);
  assert.equal(sel.extractText(s, {noSelect}), 'ab', 'chrome stripped from copy');
});

test('extractText keeps a wide glyph once (skips its continuation cell)', () => {
  const s = new Surface(4, 1);
  s.setText(0, 0, '世a'); // 世 occupies cols 0-1, a at col 2
  const sel = new Selection();
  sel.begin(0, 0);
  sel.extend(2, 0);
  assert.equal(sel.extractText(s), '世a');
});

test('extractText glues soft-wrapped rows, trimming viewport padding (no leaked spaces)', () => {
  // wrapText trims wrap points, so a rendered row's trailing spaces are viewport padding, not content
  const s = new Surface(10, 3);
  s.setText(0, 0, 'foo'); // 'foo' + 7 padding spaces
  s.setText(0, 1, 'bar');
  s.setText(0, 2, 'next');
  const softWrap = new Uint8Array(3);
  softWrap[1] = 1; // row 1 continues row 0
  const sel = new Selection();
  sel.begin(0, 0);
  sel.extend(9, 2);
  assert.equal(sel.extractText(s, {softWrap}), 'foobar\nnext', 'padding trimmed before the join');
});

test('extractText snaps to the glyph lead when selection starts on a continuation', () => {
  const s = new Surface(6, 1);
  s.setText(0, 0, 'a中b'); // 中 lead col1, continuation col2
  const sel = new Selection();
  sel.begin(2, 0); // drag begins on the continuation column
  sel.extend(3, 0);
  assert.equal(sel.extractText(s), '中b', 'wide glyph not dropped');
});

test('overlay highlights the wide-glyph lead when only its continuation is in range', () => {
  const s = new Surface(6, 1);
  s.setText(0, 0, 'a中b'); // 中 lead col1, continuation col2
  const pal = new Palette();
  const sel = new Selection();
  sel.begin(2, 0); // continuation column
  sel.extend(3, 0);
  sel.overlay(s, pal);
  assert.equal(pal.get(s.getStyle(1, 0)).inverse, true, 'lead (the drawn glyph) highlighted');
});

test('overlay toggles inverse on selected cells and skips NoSelect', () => {
  const s = grid(['abcd']);
  const pal = new Palette();
  const noSelect = new Uint8Array(s.width * s.height);
  noSelect[0] = 1; // 'a' is chrome
  const sel = new Selection();
  sel.begin(0, 0);
  sel.extend(2, 0); // a,b,c
  sel.overlay(s, pal, noSelect);
  assert.equal(pal.get(s.getStyle(0, 0)).inverse, undefined, 'NoSelect cell not highlighted');
  assert.equal(pal.get(s.getStyle(1, 0)).inverse, true, 'b highlighted');
  assert.equal(pal.get(s.getStyle(2, 0)).inverse, true, 'c highlighted');
  assert.equal(pal.get(s.getStyle(3, 0)).inverse, undefined, 'outside selection untouched');
});

test('clear removes the selection', () => {
  const sel = new Selection();
  sel.begin(0, 0);
  sel.extend(3, 0);
  sel.clear();
  assert.equal(sel.hasSelection, false);
  assert.equal(sel.range, null);
});
