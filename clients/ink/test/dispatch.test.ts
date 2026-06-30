import test from 'node:test';
import assert from 'node:assert/strict';
import {dispatchTo, pathToRoot, MouseDispatcher, type DispatchEvent} from '../src/render/dispatch.js';
import {createVNode, appendChild, setHandler, type VNode} from '../src/render/vnode.js';
import {createConfig, attachYoga, calculate, freeYoga} from '../src/render/layout.js';
import {GeomCache} from '../src/render/geomcache.js';
import type {MouseEvent} from '../src/render/hittest.js';

function mouse(over: Partial<MouseEvent>): MouseEvent {
  return {x: 0, y: 0, action: 'down', button: 'left', shift: false, meta: false, ctrl: false, ...over};
}

function ev(target: VNode, type: DispatchEvent['type'] = 'down'): DispatchEvent {
  const e: DispatchEvent = {
    type, x: 0, y: 0, button: 'left', shift: false, meta: false, ctrl: false,
    target, currentTarget: target, propagationStopped: false,
    stopPropagation(): void {
      e.propagationStopped = true;
    },
  };
  return e;
}

test('capture runs root→target, then bubble runs target→root', () => {
  const root = createVNode('ink-root');
  const mid = createVNode('ink-box');
  const leaf = createVNode('ink-box');
  appendChild(root, mid);
  appendChild(mid, leaf);
  const order: string[] = [];
  setHandler(root, 'onMouseDownCapture', () => order.push('root:capture'));
  setHandler(mid, 'onMouseDownCapture', () => order.push('mid:capture'));
  setHandler(leaf, 'onMouseDown', () => order.push('leaf:bubble'));
  setHandler(mid, 'onMouseDown', () => order.push('mid:bubble'));
  setHandler(root, 'onMouseDown', () => order.push('root:bubble'));

  dispatchTo(pathToRoot(leaf), ev(leaf), 'onMouseDown');
  assert.deepEqual(order, ['root:capture', 'mid:capture', 'leaf:bubble', 'mid:bubble', 'root:bubble']);
});

test('stopPropagation in bubble halts the remaining bubble handlers', () => {
  const root = createVNode('ink-root');
  const leaf = createVNode('ink-box');
  appendChild(root, leaf);
  const order: string[] = [];
  setHandler(leaf, 'onMouseDown', (e) => {
    order.push('leaf');
    (e as DispatchEvent).stopPropagation();
  });
  setHandler(root, 'onMouseDown', () => order.push('root'));

  dispatchTo(pathToRoot(leaf), ev(leaf), 'onMouseDown');
  assert.deepEqual(order, ['leaf'], 'root bubble suppressed');
});

test('stopPropagation in capture prevents target/bubble', () => {
  const root = createVNode('ink-root');
  const leaf = createVNode('ink-box');
  appendChild(root, leaf);
  const order: string[] = [];
  setHandler(root, 'onMouseDownCapture', (e) => {
    order.push('root:capture');
    (e as DispatchEvent).stopPropagation();
  });
  setHandler(leaf, 'onMouseDown', () => order.push('leaf:bubble'));

  dispatchTo(pathToRoot(leaf), ev(leaf), 'onMouseDown');
  assert.deepEqual(order, ['root:capture'], 'bubble never runs');
});

test('handler receives target, currentTarget and coords', () => {
  const root = createVNode('ink-root');
  const leaf = createVNode('ink-box');
  appendChild(root, leaf);
  let seen: DispatchEvent | null = null;
  setHandler(root, 'onMouseDown', (e) => {
    seen = e as DispatchEvent;
  });
  const e = ev(leaf);
  e.x = 7;
  e.y = 2;
  dispatchTo(pathToRoot(leaf), e, 'onMouseDown');
  assert.equal(seen!.target, leaf, 'target stays the origin node');
  assert.equal(seen!.currentTarget, root, 'currentTarget is the running node');
  assert.equal(seen!.x, 7);
  assert.equal(seen!.y, 2);
});

test('MouseDispatcher synthesizes onClick on press+release over the same node', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {width: 6, height: 2});
  appendChild(root, box);
  attachYoga(root, createConfig());
  calculate(root, 10, 3);
  const cache = new GeomCache();
  cache.build(root);

  const log: string[] = [];
  setHandler(box, 'onMouseDown', () => log.push('down'));
  setHandler(box, 'onMouseUp', () => log.push('up'));
  setHandler(box, 'onClick', () => log.push('click'));

  const d = new MouseDispatcher(root, cache);
  d.handle(mouse({action: 'down', x: 1, y: 0}));
  d.handle(mouse({action: 'up', x: 2, y: 0}));
  assert.deepEqual(log, ['down', 'up', 'click']);
  freeYoga(root);
});

test('MouseDispatcher does not synthesize click when release lands elsewhere', () => {
  const root = createVNode('ink-root');
  const a = createVNode('ink-box', {position: 'absolute', width: 3, height: 1});
  const b = createVNode('ink-box', {position: 'absolute', width: 3, height: 1, marginLeft: 4});
  appendChild(root, a);
  appendChild(root, b);
  attachYoga(root, createConfig());
  calculate(root, 10, 2);
  const cache = new GeomCache();
  cache.build(root);

  const log: string[] = [];
  setHandler(a, 'onClick', () => log.push('click-a'));
  setHandler(b, 'onClick', () => log.push('click-b'));

  const d = new MouseDispatcher(root, cache);
  d.handle(mouse({action: 'down', x: 1, y: 0})); // over a
  d.handle(mouse({action: 'up', x: 5, y: 0})); // over b
  assert.deepEqual(log, [], 'no click when down and up targets differ');
  freeYoga(root);
});

test('MouseDispatcher routes wheel to onWheel', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {width: 6, height: 2});
  appendChild(root, box);
  attachYoga(root, createConfig());
  calculate(root, 10, 3);
  const cache = new GeomCache();
  cache.build(root);

  let wheel = '';
  setHandler(box, 'onWheel', (e) => {
    wheel = (e as DispatchEvent).type;
  });
  new MouseDispatcher(root, cache).handle(mouse({action: 'wheelDown', button: 'none', x: 1, y: 1}));
  assert.equal(wheel, 'wheelDown');
  freeYoga(root);
});
