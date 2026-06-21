import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {FocusManager, FocusContext, useFocus, useFocusManager, handleFocusKey} from '../src/render/focus.js';
import {emptyKey} from '../src/render/hooks.js';
import {createRoot} from '../src/render/host.js';
import {createVNode} from '../src/render/vnode.js';

const h = React.createElement;

test('first registered focusable auto-focuses', () => {
  const m = new FocusManager();
  m.register('a');
  m.register('b');
  assert.equal(m.active, 'a');
  assert.equal(m.isFocused('a'), true);
  assert.equal(m.isFocused('b'), false);
});

test('focusNext / focusPrevious cycle in registration order and wrap', () => {
  const m = new FocusManager();
  m.register('a');
  m.register('b');
  m.register('c');
  m.focusNext();
  assert.equal(m.active, 'b');
  m.focusNext();
  assert.equal(m.active, 'c');
  m.focusNext();
  assert.equal(m.active, 'a', 'wraps to start');
  m.focusPrevious();
  assert.equal(m.active, 'c', 'wraps to end');
});

test('focus(id) only honors active focusables', () => {
  const m = new FocusManager();
  m.register('a');
  m.register('b', false); // inactive
  m.focus('b');
  assert.equal(m.active, 'a', 'cannot focus an inactive node');
  m.focus('a');
  assert.equal(m.active, 'a');
});

test('inactive focusables are skipped by the cycle', () => {
  const m = new FocusManager();
  m.register('a');
  m.register('b', false);
  m.register('c');
  m.focusNext();
  assert.equal(m.active, 'c', 'b skipped');
});

test('disable hides focus; enable restores it', () => {
  const m = new FocusManager();
  m.register('a');
  assert.equal(m.isFocused('a'), true);
  m.disable();
  assert.equal(m.isFocused('a'), false);
  assert.equal(m.active, null);
  m.enable();
  assert.equal(m.isFocused('a'), true);
});

test('unregistering the active node moves focus to a survivor', () => {
  const m = new FocusManager();
  m.register('a');
  m.register('b');
  assert.equal(m.active, 'a');
  m.unregister('a');
  assert.equal(m.active, 'b', 'focus moved off the removed node');
  m.unregister('b');
  assert.equal(m.active, null, 'no focusables left');
});

test('subscribe is notified on focus change and can unsubscribe', () => {
  const m = new FocusManager();
  m.register('a');
  m.register('b');
  let hits = 0;
  const off = m.subscribe(() => hits++);
  m.focusNext();
  assert.equal(hits, 1);
  off();
  m.focusNext();
  assert.equal(hits, 1, 'no more notifications after unsubscribe');
});

test('handleFocusKey: Tab→next, Shift+Tab→prev, others ignored', () => {
  const m = new FocusManager();
  m.register('a');
  m.register('b');
  assert.equal(handleFocusKey(m, emptyKey({tab: true})), true);
  assert.equal(m.active, 'b');
  assert.equal(handleFocusKey(m, emptyKey({tab: true, shift: true})), true);
  assert.equal(m.active, 'a');
  assert.equal(handleFocusKey(m, emptyKey({return: true})), false, 'non-Tab not consumed');
  assert.equal(handleFocusKey(m, emptyKey({tab: true, ctrl: true})), false, 'Ctrl+Tab left for the app');
});

test('handleFocusKey: Tab is NOT consumed when nothing is focusable (#17 — falls through to useInput)', () => {
  const m = new FocusManager(); // nothing registered → the slash-menu Tab-completion must receive Tab
  assert.equal(m.hasFocusables(), false);
  assert.equal(handleFocusKey(m, emptyKey({tab: true})), false, 'Tab left for the app when nothing is focusable');
  const m2 = new FocusManager();
  m2.register('a');
  m2.disable(); // focus turned off → Tab again belongs to the app
  assert.equal(handleFocusKey(m2, emptyKey({tab: true})), false, 'disabled focus → Tab not consumed');
});

test('useFocus registers/unregisters with the manager via the context', () => {
  const manager = new FocusManager();
  function Field(): React.ReactElement {
    const {isFocused} = useFocus();
    return h('ink-text', null, isFocused ? '[x]' : '[ ]');
  }
  const root = createRoot(createVNode('ink-root'));
  root.render(h(FocusContext.Provider, {value: manager}, h(Field), h(Field)));
  // two fields registered; the first auto-focused
  assert.equal(manager.active !== null, true, 'a field is focused');
  root.unmount();
  assert.equal(manager.active, null, 'all unregistered on unmount');
});

test('useFocusManager exposes working controls', () => {
  const manager = new FocusManager();
  manager.register('a');
  manager.register('b');
  let next: (() => void) | null = null;
  function Ctrl(): React.ReactElement {
    const {focusNext} = useFocusManager();
    next = focusNext;
    return h('ink-text', null, 'x');
  }
  createRoot(createVNode('ink-root')).render(h(FocusContext.Provider, {value: manager}, h(Ctrl)));
  assert.equal(manager.active, 'a');
  next!();
  assert.equal(manager.active, 'b', 'focusNext from the hook moved focus');
});
