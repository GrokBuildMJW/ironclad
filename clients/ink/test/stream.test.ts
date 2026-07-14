/**
 * Hermetic byte-parity tests for the \x00TR stream parser (no server needed).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createStreamParser, stripConfirm, type ToolFrame} from '../src/net/stream.js';

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

test('stream parser — versioned model exec carries the mandatory sandbox policy', async () => {
  const frames: ToolFrame[] = [];
  const feed = createStreamParser({onText: () => {}, onTool: (f) => {
    frames.push(f);
  }});
  await feed(enc.encode(NUL + 'TR{"id":"e","name":"execute_command_sandboxed_v1","args":{"command":"echo hi"},"sandbox":"bwrap"}' + NUL));
  await feed(null);
  assert.deepEqual(frames, [{
    id: 'e', name: 'execute_command_sandboxed_v1', args: {command: 'echo hi'}, sandbox: 'bwrap',
  }]);
});

test('stripConfirm — #1281: --yes/--confirm recognised in any position, not only trailing', () => {
  assert.deepEqual(stripConfirm('project delete X --purge --yes'), {msg: 'project delete X --purge', confirm: true});
  assert.deepEqual(stripConfirm('project delete X --yes --purge'), {msg: 'project delete X --purge', confirm: true});
  assert.deepEqual(stripConfirm('project delete X --confirm'), {msg: 'project delete X', confirm: true});
  assert.deepEqual(stripConfirm('project delete X --purge'), {msg: 'project delete X --purge', confirm: false});
  assert.deepEqual(stripConfirm('normal chat'), {msg: 'normal chat', confirm: false});
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

test('stream parser — heartbeat frame (\\x00HB\\x00) dropped, text intact', async () => {
  // BUG-7: the server emits \x00HB\x00 keep-alive frames during long silent turns so the
  // HTTP body stream never idles into a timeout. The client must treat HB as a control
  // frame with no valid TR-JSON → drop it silently (no text, no tool), text around it joins.
  const texts: string[] = [];
  const frames: ToolFrame[] = [];
  const feed = createStreamParser({
    onText: (s) => texts.push(s),
    onTool: (f) => {
      frames.push(f);
    },
  });
  await feed(enc.encode('Hallo' + NUL + 'HB' + NUL + 'Welt'));
  await feed(enc.encode(NUL + 'HB' + NUL)); // a lone heartbeat between text bursts
  await feed(null);
  assert.deepEqual(texts, ['Hallo', 'Welt']);
  assert.equal(frames.length, 0);
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
