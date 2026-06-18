import test from 'node:test';
import assert from 'node:assert/strict';
import {parseKey, feedKey} from '../src/render/keys.js';
import type {Key} from '../src/render/hooks.js';

const ESC = '\x1b';

/** The key flags that are set true, for compact assertions. */
function flags(key: Key): string[] {
  return (Object.keys(key) as Array<keyof Key>).filter((k) => key[k]).sort();
}

test('enter / tab / escape / backspace', () => {
  assert.deepEqual(parseKey('\r'), {input: '', key: parseKey('\r').key});
  assert.deepEqual(flags(parseKey('\r').key), ['return']);
  assert.deepEqual(flags(parseKey('\n').key), ['return']);
  assert.deepEqual(flags(parseKey('\t').key), ['tab']);
  assert.deepEqual(flags(parseKey(ESC).key), ['escape']);
  assert.deepEqual(flags(parseKey('\x7f').key), ['backspace']);
  assert.deepEqual(flags(parseKey('\x08').key), ['backspace']);
});

test('arrow keys (CSI and SS3 forms)', () => {
  assert.deepEqual(flags(parseKey(ESC + '[A').key), ['upArrow']);
  assert.deepEqual(flags(parseKey(ESC + '[B').key), ['downArrow']);
  assert.deepEqual(flags(parseKey(ESC + '[C').key), ['rightArrow']);
  assert.deepEqual(flags(parseKey(ESC + '[D').key), ['leftArrow']);
  assert.deepEqual(flags(parseKey(ESC + 'OA').key), ['upArrow'], 'application cursor mode');
});

test('navigation: delete / pageUp / pageDown', () => {
  assert.deepEqual(flags(parseKey(ESC + '[3~').key), ['delete']);
  assert.deepEqual(flags(parseKey(ESC + '[5~').key), ['pageUp']);
  assert.deepEqual(flags(parseKey(ESC + '[6~').key), ['pageDown']);
});

test('Ctrl + letter sets ctrl and yields the letter as input', () => {
  const c = parseKey('\x03'); // Ctrl+C
  assert.equal(c.input, 'c');
  assert.deepEqual(flags(c.key), ['ctrl']);
  const a = parseKey('\x01'); // Ctrl+A
  assert.equal(a.input, 'a');
  assert.deepEqual(flags(a.key), ['ctrl']);
});

test('Ctrl bytes that alias enter/tab/backspace keep their dedicated meaning', () => {
  assert.deepEqual(flags(parseKey('\x0d').key), ['return'], 'Ctrl+M = enter');
  assert.deepEqual(flags(parseKey('\x09').key), ['tab'], 'Ctrl+I = tab');
  assert.deepEqual(flags(parseKey('\x08').key), ['backspace'], 'Ctrl+H = backspace');
});

test('Alt/Meta + key', () => {
  const m = parseKey(ESC + 'f');
  assert.equal(m.input, 'f');
  assert.deepEqual(flags(m.key), ['meta']);
});

test('xterm modifier params on arrows (shift / ctrl)', () => {
  assert.deepEqual(flags(parseKey(ESC + '[1;2A').key), ['shift', 'upArrow']);
  assert.deepEqual(flags(parseKey(ESC + '[1;5D').key), ['ctrl', 'leftArrow']);
  assert.deepEqual(flags(parseKey(ESC + '[1;6C').key), ['ctrl', 'rightArrow', 'shift']);
  assert.deepEqual(flags(parseKey(ESC + '[3;3~').key), ['delete', 'meta'], 'Alt+Delete');
});

test('printable character and paste pass through as input', () => {
  const a = parseKey('a');
  assert.equal(a.input, 'a');
  assert.deepEqual(flags(a.key), []);
  const paste = parseKey('hello world');
  assert.equal(paste.input, 'hello world');
  assert.deepEqual(flags(paste.key), []);
});

test('unrecognized CSI is swallowed (no input, no flags)', () => {
  const home = parseKey(ESC + '[H'); // home — no Ink Key field
  assert.equal(home.input, '');
  assert.deepEqual(flags(home.key), []);
});

test('feedKey emits the parsed event through the bridge callback', () => {
  const seen: Array<[string, string[]]> = [];
  feedKey('\x03', (input, key) => seen.push([input, flags(key)]));
  feedKey('x', (input, key) => seen.push([input, flags(key)]));
  assert.deepEqual(seen, [['c', ['ctrl']], ['x', []]]);
});
