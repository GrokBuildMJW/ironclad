/**
 * Hermetic byte-parity tests for the \x00TR stream parser (no server needed).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createStreamParser, type ToolFrame} from '../src/net/stream.js';

const NUL = String.fromCharCode(0);
const enc = new TextEncoder();

test('stream parser — text + tool frame toggle, split across chunks', async () => {
  const texts: string[] = [];
  const frames: ToolFrame[] = [];
  const feed = createStreamParser({
    onText: (s) => texts.push(s),
    onTool: (f) => {
      frames.push(f);
    },
  });
  await feed(enc.encode('hello'));
  await feed(enc.encode(NUL + 'TR{"id":"a","name":"read_file","args":{"path":"x"}}' + NUL + 'wor'));
  await feed(enc.encode('ld'));
  await feed(null);
  assert.deepEqual(texts, ['hello', 'world']);
  assert.equal(frames.length, 1);
  assert.deepEqual(frames[0], {id: 'a', name: 'read_file', args: {path: 'x'}});
});

test('stream parser — multi-byte UTF-8 split across chunks', async () => {
  const texts: string[] = [];
  const feed = createStreamParser({onText: (s) => texts.push(s)});
  const bytes = enc.encode('café'); // é spans 2 bytes
  await feed(bytes.slice(0, 4)); // 'caf' + first byte of é
  await feed(bytes.slice(4)); // second byte of é
  await feed(null);
  assert.equal(texts.join(''), 'café');
});

test('stream parser — malformed frame dropped, stream continues', async () => {
  const texts: string[] = [];
  const frames: ToolFrame[] = [];
  const feed = createStreamParser({
    onText: (s) => texts.push(s),
    onTool: (f) => {
      frames.push(f);
    },
  });
  await feed(enc.encode('a' + NUL + 'TR{not json' + NUL + 'b'));
  await feed(null);
  assert.deepEqual(texts, ['a', 'b']);
  assert.equal(frames.length, 0);
});
