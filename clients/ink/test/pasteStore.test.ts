import test from 'node:test';
import assert from 'node:assert/strict';
import {
  newPasteStore, isMultilinePaste, pasteLineCount, pastePlaceholder,
  storePaste, displayBuffer, expandPastes, backspace, stripSentinels,
} from '../src/ui/pasteStore.js';

const OPEN = String.fromCodePoint(0xe000);
const CLOSE = String.fromCodePoint(0xe001);

test('isMultilinePaste — LF, CRLF and lone CR all count; single line does not (#438)', () => {
  assert.equal(isMultilinePaste('a\nb'), true);
  assert.equal(isMultilinePaste('a\r\nb'), true);
  assert.equal(isMultilinePaste('a\rb\rc'), true);   // lone CR (review finding)
  assert.equal(isMultilinePaste('single line'), false);
  assert.equal(isMultilinePaste('trailing only\n'), false); // .trim() drops it → stays inline
  assert.equal(isMultilinePaste(''), false);
});

test('pasteLineCount — newlines + 1, separator-agnostic', () => {
  assert.equal(pasteLineCount('one'), 1);
  assert.equal(pasteLineCount('a\nb\nc'), 3);
  assert.equal(pasteLineCount('a\r\nb\r\nc'), 3);
  assert.equal(pasteLineCount('a\rb\rc'), 3);
});

test('pastePlaceholder — the friendly text the user sees', () => {
  assert.equal(pastePlaceholder(1, 5), '[Pasted #1 +5 lines]');
  assert.equal(pastePlaceholder(2, 12), '[Pasted #2 +12 lines]');
});

test('storePaste returns an OUT-OF-BAND token (not the friendly grammar) + stores normalized raw', () => {
  const store = newPasteStore();
  const t = storePaste(store, 'a\r\nb\rc'); // mixed separators
  assert.ok(!t.includes('[Pasted'), 'the buffer token must not be the typeable grammar');
  assert.equal(displayBuffer(t), '[Pasted #1 +3 lines]');
  assert.equal(store.blocks.get(1), 'a\nb\nc'); // CRLF/CR normalized in storage
});

test('displayBuffer — sentinel → friendly text, residual newline → ⏎', () => {
  const store = newPasteStore();
  const t = storePaste(store, 'alpha\nbeta');
  assert.equal(displayBuffer(`x ${t} y`), 'x [Pasted #1 +2 lines] y');
  assert.equal(displayBuffer('line\nbreak'), 'line ⏎ break');
});

test('expandPastes — round-trip: sentinel tokens become the stored raw text', () => {
  const store = newPasteStore();
  const t1 = storePaste(store, 'alpha\nbeta');
  const t2 = storePaste(store, 'gamma\ndelta');
  assert.equal(expandPastes(`before ${t1} mid ${t2} after`, store),
    'before alpha\nbeta mid gamma\ndelta after');
});

test('expandPastes — a TYPED literal [Pasted #1 +2 lines] is NOT expanded, even when block #1 exists', () => {
  // the core round-trip-corruption regression (review S3): the visible grammar is not the round-trip key
  const store = newPasteStore();
  const t = storePaste(store, 'SECRET\nDATA'); // real block #1
  const buffer = `${t} note: [Pasted #1 +2 lines] is the format`;
  assert.equal(expandPastes(buffer, store), 'SECRET\nDATA note: [Pasted #1 +2 lines] is the format');
});

test('expandPastes — a token whose block was reclaimed falls back to friendly text (never drops content)', () => {
  const store = newPasteStore();
  const t = storePaste(store, 'x\ny');
  store.blocks.delete(1);
  assert.equal(expandPastes(`a ${t} b`, store), 'a [Pasted #1 +2 lines] b');
});

test('backspace — clears a whole trailing sentinel token AND reclaims its block', () => {
  const store = newPasteStore();
  const t = storePaste(store, 'a\nb\nc');
  assert.ok(store.blocks.has(1));
  assert.equal(backspace(`hi ${t}`, store), 'hi ');
  assert.ok(!store.blocks.has(1), 'the deleted paste block must be reclaimed (no leak)');
});

test('backspace — a TYPED literal placeholder tail is NOT swallowed (one char only)', () => {
  // review S3: backspace must not over-delete typeable text that merely looks like a placeholder
  const store = newPasteStore();
  assert.equal(backspace('msg [Pasted #1 +4 lines]', store), 'msg [Pasted #1 +4 lines');
  assert.equal(backspace('hello', store), 'hell');
  assert.equal(backspace('', store), '');
});

test('stripSentinels — a paste cannot smuggle a forged sentinel token into the buffer', () => {
  // N12 (post-hardening review): the user cannot TYPE U+E000/E001, but a paste could carry them.
  const forged = `${OPEN}1:999${CLOSE}`;
  assert.equal(stripSentinels(`x${forged}y`), 'x1:999y'); // delimiters removed, digits left as plain text
  // so even with a real block #1 present, stripped-then-stored input never re-expands the forgery
  const store = newPasteStore();
  storePaste(store, 'real\nblock'); // block #1
  const sanitized = stripSentinels(forged); // what App appends to the buffer
  assert.equal(expandPastes(sanitized, store), '1:999'); // no sentinel left → no expansion
});

test('ids are stable: reclaiming an earlier block leaves later tokens resolvable', () => {
  const store = newPasteStore();
  const t1 = storePaste(store, 'first\npaste');  // #1
  const t2 = storePaste(store, 'second\npaste'); // #2
  backspace(t1, store);                          // a trailing-token backspace reclaims block #1
  assert.ok(!store.blocks.has(1) && store.blocks.has(2));
  assert.equal(expandPastes(`x ${t2}`, store), 'x second\npaste'); // #2 still maps correctly (no reindex)
});
