import test from 'node:test';
import assert from 'node:assert/strict';
import {createVNode, createTextNode, appendChild, type VNode} from '../src/render/vnode.js';
import {createConfig, attachYoga, calculate, freeYoga} from '../src/render/layout.js';
import {GeomCache} from '../src/render/geomcache.js';

function layout(root: VNode, width: number): void {
  attachYoga(root, createConfig());
  calculate(root, width);
}

test('build records absolute rects for the whole tree', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {borderStyle: 'single', width: 6, height: 3});
  const txt = createVNode('ink-text');
  appendChild(txt, createTextNode('x'));
  appendChild(box, txt);
  appendChild(root, box);
  layout(root, 20);

  const cache = new GeomCache();
  cache.build(root);

  assert.deepEqual(cache.get(root), {x: 0, y: 0, w: 20, h: 3});
  assert.deepEqual(cache.get(box), {x: 0, y: 0, w: 6, h: 3});
  // border reserves 1 cell each edge → text content origin is (1,1), width 6-2=4
  assert.deepEqual(cache.get(txt), {x: 1, y: 1, w: 4, h: 1});
  freeYoga(root);
});

test('contamination is tracked separately from rects and resets per frame', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {width: 4, height: 1});
  appendChild(root, box);
  layout(root, 10);

  const cache = new GeomCache();
  cache.build(root);
  assert.equal(cache.isContaminated(box), false);

  cache.contaminate(box);
  assert.equal(cache.isContaminated(box), true);

  cache.resetContamination();
  assert.equal(cache.isContaminated(box), false);
  assert.ok(cache.get(box), 'rects survive a contamination reset');
  freeYoga(root);
});

test('build clears prior cache entries', () => {
  const root = createVNode('ink-root');
  appendChild(root, createVNode('ink-box', {width: 2, height: 1}));
  layout(root, 10);

  const cache = new GeomCache();
  const stale = createVNode('ink-box');
  cache.set(stale, {x: 9, y: 9, w: 1, h: 1});
  cache.build(root);
  assert.equal(cache.has(stale), false, 'stale entry dropped on rebuild');
  freeYoga(root);
});

test('a node without a Yoga node (and its subtree) is skipped', () => {
  const root = createVNode('ink-root');
  const laid = createVNode('ink-box', {width: 2, height: 1});
  appendChild(root, laid);
  layout(root, 10); // attaches yoga to root + laid only

  const orphan = createVNode('ink-box', {width: 2, height: 1});
  appendChild(orphan, createVNode('ink-text'));
  appendChild(root, orphan); // appended AFTER layout → no yoga node

  const cache = new GeomCache();
  cache.build(root);
  assert.ok(cache.has(laid), 'laid-out node recorded');
  assert.equal(cache.has(orphan), false, 'un-laid-out node skipped');
  assert.equal(cache.has(orphan.children[0] as VNode), false, 'its subtree skipped too');
  freeYoga(root);
});

test('flexGrow fills a height-constrained column so the last child pins to the bottom row', () => {
  const root = createVNode('ink-root');
  const col = createVNode('ink-box', {flexDirection: 'column', flexGrow: 1});
  const grow = createVNode('ink-box', {flexGrow: 1}); // transcript region fills the slack
  appendChild(grow, createVNode('ink-text'));
  appendChild(col, grow);
  const bottom = createVNode('ink-text');
  appendChild(bottom, createTextNode('B'));
  appendChild(col, bottom);
  appendChild(root, col);
  attachYoga(root, createConfig());
  calculate(root, 20, 5); // height-constrained to 5 rows

  const cache = new GeomCache();
  cache.build(root);
  assert.equal(cache.get(bottom)?.y, 4, 'last child (footer/input) pinned to the bottom row');
  freeYoga(root);
});

test('marginTop offsets a child down, leaving a blank row above it', () => {
  const root = createVNode('ink-root'); // default column
  const a = createVNode('ink-text');
  appendChild(a, createTextNode('A'));
  const b = createVNode('ink-box', {marginTop: 1});
  const bt = createVNode('ink-text');
  appendChild(bt, createTextNode('B'));
  appendChild(b, bt);
  appendChild(root, a);
  appendChild(root, b);
  attachYoga(root, createConfig());
  calculate(root, 20);

  const cache = new GeomCache();
  cache.build(root);
  assert.equal(cache.get(a)?.y, 0, 'first row');
  assert.equal(cache.get(b)?.y, 2, 'pushed to row 2 (row 1 is the margin gap)');
  freeYoga(root);
});

test('delete drops both the rect and any contamination', () => {
  const cache = new GeomCache();
  const n = createVNode('ink-box');
  cache.set(n, {x: 0, y: 0, w: 1, h: 1});
  cache.contaminate(n);
  cache.delete(n);
  assert.equal(cache.has(n), false);
  assert.equal(cache.isContaminated(n), false);
});
