import test from 'node:test';
import assert from 'node:assert/strict';
import {parseMouse, hitTest} from '../src/render/hittest.js';
import {createVNode, appendChild, type VNode} from '../src/render/vnode.js';
import {createConfig, attachYoga, calculate, freeYoga} from '../src/render/layout.js';
import {GeomCache} from '../src/render/geomcache.js';

const ESC = '\x1b';

test('parseMouse decodes a left press to 0-based coords', () => {
  const e = parseMouse(ESC + '[<0;5;3M');
  assert.equal(e?.action, 'down');
  assert.equal(e?.button, 'left');
  assert.equal(e?.x, 4); // col 5 → x 4
  assert.equal(e?.y, 2); // row 3 → y 2
});

test('parseMouse decodes release (m) and right button', () => {
  const up = parseMouse(ESC + '[<0;1;1m');
  assert.equal(up?.action, 'up');
  const right = parseMouse(ESC + '[<2;1;1M');
  assert.equal(right?.button, 'right');
  assert.equal(right?.action, 'down');
});

test('parseMouse decodes motion/drag (bit 32)', () => {
  const drag = parseMouse(ESC + '[<32;10;10M'); // left button held + motion
  assert.equal(drag?.action, 'move');
  assert.equal(drag?.button, 'left');
});

test('parseMouse decodes wheel up/down (bit 64)', () => {
  assert.equal(parseMouse(ESC + '[<64;1;1M')?.action, 'wheelUp');
  assert.equal(parseMouse(ESC + '[<65;1;1M')?.action, 'wheelDown');
  assert.equal(parseMouse(ESC + '[<64;1;1M')?.button, 'none');
});

test('parseMouse decodes modifier bits', () => {
  const e = parseMouse(ESC + '[<28;1;1M'); // 0 + shift(4) + meta(8) + ctrl(16)
  assert.equal(e?.shift, true);
  assert.equal(e?.meta, true);
  assert.equal(e?.ctrl, true);
});

test('parseMouse returns null for non-mouse input', () => {
  assert.equal(parseMouse(ESC + '[A'), null);
  assert.equal(parseMouse('a'), null);
});

test('hitTest returns the topmost (deepest) node containing the cell', () => {
  const root = createVNode('ink-root');
  const outer = createVNode('ink-box', {width: 10, height: 4, paddingLeft: 2, paddingTop: 1});
  const inner = createVNode('ink-box', {width: 3, height: 1});
  appendChild(outer, inner);
  appendChild(root, outer);
  attachYoga(root, createConfig());
  calculate(root, 20);

  const cache = new GeomCache();
  cache.build(root);
  // inner sits at (2,1) size 3x1 inside outer
  assert.equal(hitTest(root, cache, 3, 1), inner, 'point inside inner → inner');
  assert.equal(hitTest(root, cache, 0, 0), outer, 'point in outer padding (not inner) → outer');
  assert.equal(hitTest(root, cache, 50, 50), null, 'point outside everything → null');
  freeYoga(root);
});

test('hitTest picks the later sibling when rects overlap (paint order)', () => {
  const root = createVNode('ink-root');
  // two absolutely-positioned boxes overlapping at (0,0)
  const a = createVNode('ink-box', {position: 'absolute', width: 4, height: 2});
  const b = createVNode('ink-box', {position: 'absolute', width: 4, height: 2});
  appendChild(root, a);
  appendChild(root, b); // painted after a → on top
  attachYoga(root, createConfig());
  calculate(root, 20, 5);

  const cache = new GeomCache();
  cache.build(root);
  assert.equal(hitTest(root, cache, 1, 1), b, 'overlapping → later-painted sibling wins');
  freeYoga(root);
});
