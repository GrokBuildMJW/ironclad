import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {createRoot} from '../src/render/host.js';
import {createVNode, type VNode, type TextNode} from '../src/render/vnode.js';

const h = React.createElement;

test('reconciler builds the vnode tree (box > text > textnode)', () => {
  const container = createVNode('ink-root');
  createRoot(container).render(h('ink-box', {flexDirection: 'row'}, h('ink-text', null, 'hi')));
  const box = container.children[0] as VNode;
  assert.equal(box.kind, 'element');
  assert.equal(box.type, 'ink-box');
  assert.equal(box.props['flexDirection'], 'row');
  const text = box.children[0] as VNode;
  assert.equal(text.type, 'ink-text');
  const tn = text.children[0] as TextNode;
  assert.equal(tn.kind, 'text');
  assert.equal(tn.value, 'hi');
});

test('on* props become handlers, not visual props', () => {
  const container = createVNode('ink-root');
  const fn = (): void => {};
  createRoot(container).render(h('ink-box', {color: 'red', onClick: fn}));
  const box = container.children[0] as VNode;
  assert.equal(box.props['color'], 'red');
  assert.equal(box.props['onClick'], undefined, 'handler not in props');
  assert.equal(box.handlers['onClick'], fn);
});

test('commitUpdate — re-render updates props on the SAME instance', () => {
  const container = createVNode('ink-root');
  const root = createRoot(container);
  root.render(h('ink-box', {color: 'red'}));
  const box = container.children[0] as VNode;
  root.render(h('ink-box', {color: 'blue'}));
  assert.equal(container.children[0], box, 'same instance reused');
  assert.equal(box.props['color'], 'blue');
});

test('commitTextUpdate — text change in place', () => {
  const container = createVNode('ink-root');
  const root = createRoot(container);
  root.render(h('ink-text', null, 'a'));
  const tn = (container.children[0] as VNode).children[0] as TextNode;
  root.render(h('ink-text', null, 'b'));
  assert.equal(tn.value, 'b');
});

test('removeChild — conditional child unmounts', () => {
  const container = createVNode('ink-root');
  const root = createRoot(container);
  root.render(h('ink-box', null, h('ink-text', null, 'x'), h('ink-text', null, 'y')));
  const box = container.children[0] as VNode;
  assert.equal(box.children.length, 2);
  root.render(h('ink-box', null, h('ink-text', null, 'x')));
  assert.equal(box.children.length, 1);
});

test('commitHook fires after a commit', () => {
  const container = createVNode('ink-root');
  let calls = 0;
  const root = createRoot(container, () => {
    calls++;
  });
  root.render(h('ink-box', null));
  assert.ok(calls >= 1, 'commit hook ran');
});
