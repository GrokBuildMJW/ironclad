import test from 'node:test';
import assert from 'node:assert/strict';
import {ScrollBox} from '../src/render/scrollbox.js';
import {emptyKey} from '../src/render/hooks.js';

test('starts stuck to the bottom and follows growing content', () => {
  const sb = new ScrollBox(10, 10);
  assert.equal(sb.atBottom, true);
  assert.equal(sb.top, 0); // content fits → max 0
  sb.setContentHeight(30);
  assert.equal(sb.max, 20);
  assert.equal(sb.top, 20, 'followed to the new bottom');
  assert.equal(sb.stickToBottom, true);
});

test('scrolling up disengages stick and freezes the view as content grows', () => {
  const sb = new ScrollBox(10, 30); // max 20, parked at 20
  sb.scrollBy(-5); // up to 15
  assert.equal(sb.top, 15);
  assert.equal(sb.stickToBottom, false);
  sb.setContentHeight(50); // content grows; frozen view stays put
  assert.equal(sb.top, 15, 'view frozen while scrolled up');
});

test('returning to the bottom re-engages stick', () => {
  const sb = new ScrollBox(10, 30);
  sb.scrollBy(-5);
  assert.equal(sb.stickToBottom, false);
  sb.toBottom();
  assert.equal(sb.atBottom, true);
  assert.equal(sb.stickToBottom, true);
  sb.setContentHeight(40);
  assert.equal(sb.top, sb.max, 'follows again');
});

test('clamps at top and bottom', () => {
  const sb = new ScrollBox(10, 30);
  sb.scrollBy(-1000);
  assert.equal(sb.top, 0);
  assert.equal(sb.atTop, true);
  sb.scrollBy(1000);
  assert.equal(sb.top, 20);
  assert.equal(sb.atBottom, true);
});

test('pageUp/pageDown move by viewport minus one overlap row', () => {
  const sb = new ScrollBox(10, 100); // max 90, at 90
  sb.pageUp();
  assert.equal(sb.top, 90 - 9);
  sb.pageDown();
  assert.equal(sb.top, 90);
});

test('half-page Ctrl+U / Ctrl+D', () => {
  const sb = new ScrollBox(10, 100);
  sb.onKey(emptyKey({ctrl: true}), 'u');
  assert.equal(sb.top, 90 - 5);
  sb.onKey(emptyKey({ctrl: true}), 'd');
  assert.equal(sb.top, 90);
});

test('vi keys j/k/g/G', () => {
  const sb = new ScrollBox(10, 100);
  assert.equal(sb.onKey(emptyKey(), 'k'), true);
  assert.equal(sb.top, 89);
  sb.onKey(emptyKey(), 'j');
  assert.equal(sb.top, 90);
  sb.onKey(emptyKey(), 'g');
  assert.equal(sb.top, 0);
  sb.onKey(emptyKey(), 'G');
  assert.equal(sb.top, 90);
  assert.equal(sb.stickToBottom, true);
});

test('onWheel scrolls by a step and is always consumed', () => {
  const sb = new ScrollBox(10, 100);
  assert.equal(sb.onWheel('wheelUp'), true);
  assert.equal(sb.top, 87);
  sb.onWheel('wheelDown');
  assert.equal(sb.top, 90);
});

test('visibleRange and isVisible reflect the viewport', () => {
  const sb = new ScrollBox(10, 100);
  sb.scrollTo(40);
  assert.deepEqual(sb.visibleRange(), {top: 40, bottom: 50});
  assert.equal(sb.isVisible(45), true);
  assert.equal(sb.isVisible(39), false);
  assert.equal(sb.isVisible(50), false);
});

test('PageUp/PageDown keys are consumed; unrelated keys are not', () => {
  const sb = new ScrollBox(10, 100);
  assert.equal(sb.onKey(emptyKey({pageUp: true})), true);
  assert.equal(sb.onKey(emptyKey({pageDown: true})), true);
  assert.equal(sb.onKey(emptyKey({return: true})), false);
  assert.equal(sb.onKey(emptyKey(), 'x'), false);
});
