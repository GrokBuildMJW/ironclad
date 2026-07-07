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

test('route — #1181 the engine "⋯ …" transient status is dropped, never committed', () => {
  const r = createRouter();
  r.route('  ⋯ Qwen erzeugt Tool-Aufruf …');
  assert.equal(r.answer.length, 0, 'the "⋯" status line does not reach the transcript');
  r.route('real answer');
  assert.equal(answerBody(r), 'real answer');
});

test('route — #453 [agent] goes to the agent status, not the answer', () => {
  const r = createRouter();
  assert.equal(r.agent, ''); // a fresh per-turn router has no stale coder (no carry-over between turns)
  r.route('  [agent] codex · cheapest-capable');
  assert.equal(r.agent, 'codex · cheapest-capable');
  assert.equal(r.answer.length, 0);
  r.route('  [agent] spark-vllm · local-idle'); // last-wins (the most recently routed coder)
  assert.equal(r.agent, 'spark-vllm · local-idle');
  assert.equal(r.answer.length, 0);
});

test('route — #505 S9 [search] goes to the search status, not the answer', () => {
  const r = createRouter();
  assert.equal(r.search, ''); // fresh per-turn router carries no stale web-search summary
  r.route('  [search] n=2 ms=153');
  assert.equal(r.search, 'n=2 ms=153');
  assert.equal(r.answer.length, 0); // stripped from the chat (footer chip only)
});

test('route — DONE banner + role labels are dropped', () => {
  const r = createRouter();
  r.route('  ======== ✓ DONE · ready · 1 gen · 2s · 117 tok ========');
  r.route('[GX10]');
  r.route('  [Qwen (planning)]');
  assert.equal(r.answer.length, 0);
});

test('route — MPR report sentinels are dropped (raw + indented/glued END)', () => {
  const r = createRouter();
  r.route('<<<MPR_REPORT>>>');
  r.route('| Kriterium | A | B |');
  r.route('        <<<END>>>'); // the model often indents it or glues it to the last bullet
  assert.deepEqual(r.answer, ['| Kriterium | A | B |']);
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
