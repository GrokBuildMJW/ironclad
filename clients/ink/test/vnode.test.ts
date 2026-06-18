import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createVNode,
  createTextNode,
  appendChild,
  insertBefore,
  removeChild,
  markDirty,
  clearDirty,
  updateProps,
  setHandler,
  removeHandler,
  setText,
} from '../src/render/vnode.js';

test('createVNode — starts dirty, empty children/handlers/props, no yoga', () => {
  const n = createVNode('ink-box', {flexDirection: 'row'});
  assert.equal(n.kind, 'element');
  assert.equal(n.type, 'ink-box');
  assert.equal(n.dirty, true);
  assert.deepEqual(n.children, []);
  assert.deepEqual(n.handlers, {});
  assert.equal(n.yoga, null);
  assert.equal(n.props['flexDirection'], 'row');
});

test('appendChild — links parent/child and dirties the parent', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box');
  clearDirty(root);
  appendChild(root, box);
  assert.equal(box.parent, root);
  assert.deepEqual(root.children, [box]);
  assert.equal(root.dirty, true);
});

test('markDirty — propagates to the root, NOT into sibling subtrees', () => {
  const root = createVNode('ink-root');
  const a = createVNode('ink-box');
  const b = createVNode('ink-box');
  const aText = createTextNode('hi');
  appendChild(root, a);
  appendChild(root, b);
  appendChild(a, aText);
  [root, a, b].forEach(clearDirty);

  markDirty(aText); // change deep inside subtree a
  assert.equal(a.dirty, true, 'a (path to root) is dirty');
  assert.equal(root.dirty, true, 'root is dirty');
  assert.equal(b.dirty, false, 'sibling subtree b stays clean');
});

test('setHandler — does NOT dirty the node (handler identity churns per render)', () => {
  const n = createVNode('ink-box');
  clearDirty(n);
  setHandler(n, 'onInput', () => {});
  assert.equal(n.dirty, false, 'handler update must not dirty');
  assert.equal(typeof n.handlers['onInput'], 'function');
  assert.deepEqual(Object.keys(n.props), [], 'handler is NOT in props');
  removeHandler(n, 'onInput');
  assert.equal(n.handlers['onInput'], undefined);
  assert.equal(n.dirty, false);
});

test('updateProps / setText — DO dirty (visual change)', () => {
  const box = createVNode('ink-box');
  const txt = createTextNode('a');
  appendChild(box, txt);
  clearDirty(box);
  updateProps(box, {color: 'red'});
  assert.equal(box.dirty, true);
  assert.equal(box.props['color'], 'red');

  clearDirty(box);
  setText(txt, 'b');
  assert.equal(txt.value, 'b');
  assert.equal(box.dirty, true, 'text change dirties the parent');
});

test('insertBefore / removeChild — maintain order + parent links', () => {
  const root = createVNode('ink-root');
  const a = createVNode('ink-box');
  const b = createVNode('ink-box');
  const c = createVNode('ink-box');
  appendChild(root, a);
  appendChild(root, c);
  insertBefore(root, b, c);
  assert.deepEqual(root.children, [a, b, c]);
  removeChild(root, b);
  assert.deepEqual(root.children, [a, c]);
  assert.equal(b.parent, null);
});

test('appendChild — moving a node detaches it from its old parent first', () => {
  const p1 = createVNode('ink-box');
  const p2 = createVNode('ink-box');
  const child = createVNode('ink-text');
  appendChild(p1, child);
  appendChild(p2, child); // move
  assert.deepEqual(p1.children, [], 'detached from old parent');
  assert.deepEqual(p2.children, [child]);
  assert.equal(child.parent, p2);
});
