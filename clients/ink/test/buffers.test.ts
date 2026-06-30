import test from 'node:test';
import assert from 'node:assert/strict';
import {Buffers} from '../src/render/buffers.js';

test('front and back are distinct surfaces', () => {
  const b = new Buffers(10, 4);
  assert.notEqual(b.front, b.back);
  assert.equal(b.back.width, 10);
  assert.equal(b.front.height, 4);
});

test('swap() flips front and back (pointer swap)', () => {
  const b = new Buffers(8, 2);
  const back0 = b.back;
  const front0 = b.front;
  b.swap();
  assert.equal(b.front, back0, 'old back is the new front');
  assert.equal(b.back, front0, 'old front is the new back');
});

test('writes to back do not touch the front until swap', () => {
  const b = new Buffers(6, 1);
  b.back.setText(0, 0, 'hi');
  assert.equal(b.back.getChar(0, 0), 'h');
  assert.equal(b.front.getChar(0, 0), ' ', 'front untouched');
  b.swap();
  assert.equal(b.front.getChar(0, 0), 'h', 'composed frame is now on the front');
});

test('resize resizes both surfaces and updates meta', () => {
  const b = new Buffers(10, 4);
  b.resize(20, 6);
  assert.equal(b.width, 20);
  assert.equal(b.height, 6);
  assert.equal(b.back.width, 20);
  assert.equal(b.front.width, 20);
  assert.equal(b.back.height, 6);
  assert.equal(b.front.height, 6);
  assert.equal(b.meta.width, 20);
  assert.equal(b.meta.height, 6);
});

test('degenerate sizes are clamped, not negative', () => {
  const b = new Buffers(-5, -2);
  assert.equal(b.width, 0);
  assert.equal(b.height, 0);
  assert.equal(b.back.width, 0);
});
