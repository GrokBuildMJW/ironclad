import test from 'node:test';
import assert from 'node:assert/strict';
import {classify, LOCAL_COMMANDS} from '../src/commands.js';

test('classify — empty / whitespace', () => {
  assert.deepEqual(classify(''), {kind: 'empty', name: '', payload: ''});
  assert.deepEqual(classify('   '), {kind: 'empty', name: '', payload: ''});
  assert.deepEqual(classify('/'), {kind: 'empty', name: '', payload: ''});
  assert.deepEqual(classify('/   '), {kind: 'empty', name: '', payload: ''});
});

test('classify — bare exit/quit → local exit (both names normalise to "exit")', () => {
  assert.deepEqual(classify('exit'), {kind: 'local', name: 'exit', payload: 'exit'});
  assert.deepEqual(classify('QUIT'), {kind: 'local', name: 'exit', payload: 'quit'});
});

test('classify — plain text → turn (verbatim payload, trimmed)', () => {
  assert.deepEqual(classify('wer bist du?'), {kind: 'turn', name: '', payload: 'wer bist du?'});
  assert.deepEqual(classify('  hello world  '), {kind: 'turn', name: '', payload: 'hello world'});
});

test('classify — local slash commands', () => {
  for (const c of LOCAL_COMMANDS) {
    if (c === 'exit' || c === 'quit') continue; // handled by the bare branch
    const r = classify(`/${c}`);
    assert.equal(r.kind, 'local', `/${c} should be local`);
    assert.equal(r.name, c);
    assert.equal(r.payload, c);
  }
  assert.deepEqual(classify('/auto on'), {kind: 'local', name: 'auto', payload: 'auto on'});
});

test('classify — server slash commands (forwarded, slash already stripped in payload)', () => {
  assert.deepEqual(classify('/status'), {kind: 'server', name: 'status', payload: 'status'});
  assert.deepEqual(classify('/ls src'), {kind: 'server', name: 'ls', payload: 'ls src'});
  // no local /doctor — must forward as a server command
  assert.deepEqual(classify('/doctor'), {kind: 'server', name: 'doctor', payload: 'doctor'});
});
