/**
 * stream/route.ts — byte-parity with cli.py's route(): [perf]→status, DONE/role-labels
 * dropped, blank runs collapsed, prose accumulated.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRouter, answerBody} from '../src/stream/route.js';

test('route — [perf] goes to status (perf + tokens), not the answer', () => {
  const r = createRouter();
  r.route('  [perf] TTFT 0.5s · 117 tok/1.9s = 62 tok/s · prompt 2802');
  assert.equal(r.tokens, 117);
  assert.match(r.perf, /TTFT 0\.5s/);
  assert.equal(r.answer.length, 0);
});

test('route — DONE banner + role labels are dropped', () => {
  const r = createRouter();
  r.route('  ======== ✓ DONE · ready · 1 gen · 2s · 117 tok ========');
  r.route('[GX10]');
  r.route('  [Qwen (planning)]');
  assert.equal(r.answer.length, 0);
});

test('route — prose accumulates, blank runs collapse to one', () => {
  const r = createRouter();
  r.route('Ich bin Ironclad.');
  r.route('');
  r.route('');
  r.route('Zweite Zeile.');
  assert.deepEqual(r.answer, ['Ich bin Ironclad.', '', 'Zweite Zeile.']);
  assert.equal(answerBody(r), 'Ich bin Ironclad.\n\nZweite Zeile.');
});

test('route — leading blank is suppressed (starts collapsed)', () => {
  const r = createRouter();
  r.route('');
  r.route('first');
  assert.deepEqual(r.answer, ['first']);
});

test('feed — line-buffers chunks (split mid-line) and routes complete lines', () => {
  const r = createRouter();
  r.feed('Hel');
  r.feed('lo\nWor');
  r.feed('ld\n  [perf] 5 tok\n');
  r.flush();
  assert.deepEqual(r.answer, ['Hello', 'World']);
  assert.equal(r.tokens, 5);
});

test('feed — flush emits a trailing partial line', () => {
  const r = createRouter();
  r.feed('only partial no newline');
  assert.equal(r.answer.length, 0);
  r.flush();
  assert.deepEqual(r.answer, ['only partial no newline']);
});
