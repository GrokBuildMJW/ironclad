/**
 * Spike B (GATE) — safeCut never splits inside an open code fence or a still-streaming
 * table; on ambiguity it returns -1 (render-once fallback).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {safeCut} from '../src/stream/safeCut.js';

test('safeCut — commits up to the last paragraph boundary', () => {
  const buf = '# H\n\nbody';
  const n = safeCut(buf);
  assert.equal(buf.slice(0, n), '# H\n\n', 'commit the completed heading paragraph');
});

test('safeCut — NEVER cuts into an open code fence (excludes the fence)', () => {
  const buf = 'text\n\n```\nconst x = 1;\nmore';
  const n = safeCut(buf);
  assert.equal(buf.slice(0, n), 'text\n\n', 'cut before the open fence');
  assert.doesNotMatch(buf.slice(0, n), /```/, 'committed prefix has no fence');
});

test('safeCut — fence at the very start → nothing safe yet (-1)', () => {
  assert.equal(safeCut('```\ncode\nmore'), -1);
});

test('safeCut — still-streaming table (no terminating blank) → -1', () => {
  assert.equal(safeCut('| a | b |\n| 1 | 2 |'), -1);
});

test('safeCut — single unterminated line → -1', () => {
  assert.equal(safeCut('no blank lines yet'), -1);
});

test('safeCut — a CLOSED fence is committable once a blank follows it', () => {
  const buf = 'intro\n\n```\ncode\n```\n\nafter';
  const n = safeCut(buf);
  assert.equal(buf.slice(0, n), 'intro\n\n```\ncode\n```\n\n', 'closed fence + blank is safe');
});

test('safeCut — fully complete buffer commits everything', () => {
  const buf = 'a\n\nb\n\n';
  assert.equal(safeCut(buf), buf.length);
});
