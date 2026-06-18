import test from 'node:test';
import assert from 'node:assert/strict';
import {Surface, WIDE, WIDE_CONT} from '../src/render/surface.js';
import {blitRect, blitNode} from '../src/render/blit.js';
import {GeomCache} from '../src/render/geomcache.js';
import {createVNode} from '../src/render/vnode.js';

function styledFrom(): Surface {
  const s = new Surface(6, 2);
  s.setText(0, 0, 'abc', 7); // style id 7 on row 0
  s.setText(0, 1, 'XYZ', 3);
  return s;
}

test('blitRect copies code + style + flags faithfully', () => {
  const from = styledFrom();
  const to = new Surface(6, 2);
  blitRect(from, to, {x: 0, y: 0, w: 3, h: 1});
  assert.equal(to.getChar(0, 0), 'a');
  assert.equal(to.getChar(2, 0), 'c');
  assert.equal(to.getStyle(1, 0), 7, 'style copied');
  assert.equal(to.getChar(0, 1), ' ', 'row outside the rect untouched');
});

test('blitRect clamps an out-of-bounds rect without crashing', () => {
  const from = styledFrom(); // 'XYZ' at row 1: X@0 Y@1 Z@2
  const to = new Surface(6, 2);
  blitRect(from, to, {x: 1, y: 1, w: 10, h: 10}); // overruns right + bottom
  assert.equal(to.getChar(1, 1), 'Y');
  assert.equal(to.getChar(2, 1), 'Z');
  assert.equal(to.getChar(5, 1), ' ', 'cells past the content are blank, not out of range');
});

test('blitRect marks damage on the destination', () => {
  const from = styledFrom();
  const to = new Surface(6, 2);
  to.resetDamage();
  blitRect(from, to, {x: 1, y: 0, w: 2, h: 1});
  const d = to.damage;
  assert.ok(d, 'damage recorded');
  assert.equal(d?.minX, 1);
  assert.equal(d?.maxX, 2);
  assert.equal(d?.minY, 0);
  assert.equal(d?.maxY, 0);
});

test('blitRect widens left to keep a wide-glyph pair whole', () => {
  const from = new Surface(6, 1);
  from.setText(0, 0, '世a'); // 世 = WIDE at 0, WIDE_CONT at 1, 'a' at 2
  const to = new Surface(6, 1);
  // rect starts on the continuation cell (x=1); must widen left to include the lead at x=0
  blitRect(from, to, {x: 1, y: 0, w: 1, h: 1});
  assert.equal(to.getChar(0, 0), '世');
  assert.equal(to.getFlag(0, 0), WIDE);
  assert.equal(to.getFlag(1, 0), WIDE_CONT);
});

test('blitRect widens right to keep a wide-glyph pair whole', () => {
  const from = new Surface(6, 1);
  from.setText(0, 0, '世a');
  const to = new Surface(6, 1);
  // rect covers only the lead (x=0); must widen right to include the continuation at x=1
  blitRect(from, to, {x: 0, y: 0, w: 1, h: 1});
  assert.equal(to.getChar(0, 0), '世');
  assert.equal(to.getFlag(0, 0), WIDE);
  assert.equal(to.getFlag(1, 0), WIDE_CONT);
});

test('blitRect blanks a destination wide-pair partner the copy severs (no under-damage ghost)', () => {
  const from = new Surface(6, 1);
  from.setText(0, 0, 'PPPPPP'); // all narrow
  const to = new Surface(6, 1);
  to.setText(2, 0, '世'); // wide pair already in the destination: lead@2, cont@3
  to.resetDamage();
  blitRect(from, to, {x: 3, y: 0, w: 3, h: 1}); // overwrites the cont@3 → would orphan the lead@2
  assert.equal(to.getFlag(2, 0), 0, 'orphaned lead flag cleared');
  assert.equal(to.getChar(2, 0), ' ', 'orphaned lead blanked');
  const d = to.damage;
  assert.ok(d && d.minX <= 2, 'severed partner pulled into the damage box so diff reconciles it');
});

test('blitRect blanks a source lead with no room in a narrower destination', () => {
  const from = new Surface(8, 1);
  from.setText(0, 0, 'aa世aaaa'); // 世 = lead@2, cont@3
  const to = new Surface(3, 1); // narrower than from
  blitRect(from, to, {x: 0, y: 0, w: 8, h: 1}); // clamps to width 3 → last copied col (2) is the lead
  assert.equal(to.getChar(0, 0), 'a');
  assert.equal(to.getChar(1, 0), 'a');
  assert.equal(to.getFlag(2, 0), 0, 'no orphan WIDE flag at the destination edge');
  assert.equal(to.getChar(2, 0), ' ', 'a lead with no room for its continuation becomes a blank');
});

test('blitNode copies a cached node and reports success', () => {
  const from = styledFrom();
  const to = new Surface(6, 2);
  const cache = new GeomCache();
  const node = createVNode('ink-box');
  cache.set(node, {x: 0, y: 0, w: 3, h: 1});
  assert.equal(blitNode(from, to, cache, node), true);
  assert.equal(to.getChar(1, 0), 'b');
});

test('blitNode refuses a contaminated node (caller must repaint)', () => {
  const from = styledFrom();
  const to = new Surface(6, 2);
  const cache = new GeomCache();
  const node = createVNode('ink-box');
  cache.set(node, {x: 0, y: 0, w: 3, h: 1});
  cache.contaminate(node);
  assert.equal(blitNode(from, to, cache, node), false);
  assert.equal(to.getChar(0, 0), ' ', 'nothing copied for a contaminated node');
});

test('blitNode refuses a node with no cached geometry', () => {
  const from = styledFrom();
  const to = new Surface(6, 2);
  const cache = new GeomCache();
  assert.equal(blitNode(from, to, cache, createVNode('ink-box')), false);
});
