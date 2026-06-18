import test from 'node:test';
import assert from 'node:assert/strict';
import {Search} from '../src/render/search.js';
import {Surface} from '../src/render/surface.js';
import {Palette} from '../src/render/palette.js';

function grid(rows: string[]): Surface {
  const w = Math.max(...rows.map((r) => r.length));
  const s = new Surface(w, rows.length);
  rows.forEach((r, y) => s.setText(0, y, r));
  return s;
}

test('find locates matches across rows with correct cell ranges', () => {
  const s = grid(['foo bar', 'bar baz']);
  const search = new Search();
  const matches = search.find(s, 'bar');
  assert.equal(matches.length, 2);
  assert.deepEqual(matches[0], {y: 0, startX: 4, endX: 6});
  assert.deepEqual(matches[1], {y: 1, startX: 0, endX: 2});
});

test('find is case-insensitive by default, case-sensitive on request', () => {
  const s = grid(['Foo foo FOO']);
  assert.equal(new Search().find(s, 'foo').length, 3);
  assert.equal(new Search().find(s, 'foo', {caseSensitive: true}).length, 1);
});

test('match cell ranges account for a preceding wide glyph', () => {
  const s = new Surface(5, 1);
  s.setText(0, 0, '世xy'); // 世 cols 0-1, x col 2, y col 3
  const m = new Search().find(s, 'xy');
  assert.deepEqual(m[0], {y: 0, startX: 2, endX: 3});
});

test('cell ranges are correct after an astral codepoint (emoji)', () => {
  const s = new Surface(6, 1);
  s.setText(0, 0, '😀ab'); // 😀 is a wide astral glyph (cols 0-1), a col2, b col3
  assert.deepEqual(new Search().find(s, 'ab')[0], {y: 0, startX: 2, endX: 3});
  const s2 = new Surface(6, 1);
  s2.setText(0, 0, 'a🚀b'); // 🚀 cols 1-2
  assert.deepEqual(new Search().find(s2, '🚀')[0], {y: 0, startX: 1, endX: 2});
});

test('cell ranges survive a length-expanding lowercase fold', () => {
  const s = new Surface(6, 1);
  s.setText(0, 0, 'aİbcd'); // İ (U+0130) lowercases to two code units
  assert.deepEqual(new Search().find(s, 'cd')[0], {y: 0, startX: 3, endX: 4});
});

test('next/previous cycle and wrap; currentIndex tracks position', () => {
  const s = grid(['a a a']);
  const search = new Search();
  search.find(s, 'a');
  assert.equal(search.count, 3);
  assert.equal(search.currentIndex, 0);
  search.next();
  assert.equal(search.currentIndex, 1);
  search.next();
  search.next();
  assert.equal(search.currentIndex, 0, 'wraps forward');
  search.previous();
  assert.equal(search.currentIndex, 2, 'wraps backward');
});

test('isMatch flags cells and marks the current match', () => {
  const s = grid(['x x']);
  const search = new Search();
  search.find(s, 'x'); // matches at col 0 and col 2; current = first
  assert.deepEqual(search.isMatch(0, 0), {match: true, current: true});
  assert.deepEqual(search.isMatch(2, 0), {match: true, current: false});
  assert.deepEqual(search.isMatch(1, 0), {match: false, current: false});
  search.next();
  assert.deepEqual(search.isMatch(2, 0), {match: true, current: true}, 'current moved');
});

test('overlay highlights matches, current one distinct', () => {
  const s = grid(['cat cat']);
  const pal = new Palette();
  const search = new Search();
  search.find(s, 'cat'); // current = first (col 0-2)
  search.overlay(s, pal);
  assert.equal(pal.get(s.getStyle(0, 0)).bg, 'magenta', 'current match style');
  assert.equal(pal.get(s.getStyle(4, 0)).bg, 'yellow', 'other match style');
  assert.equal(pal.get(s.getStyle(3, 0)).bg, undefined, 'gap not highlighted');
});

test('empty query clears matches', () => {
  const s = grid(['abc']);
  const search = new Search();
  search.find(s, 'a');
  assert.equal(search.count, 1);
  search.find(s, '');
  assert.equal(search.count, 0);
  assert.equal(search.current, null);
});
