import test, {afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {
  emitDiagnosticOnce,
  resetDiagnosticsForTest,
  setDiagnosticSink,
} from '../src/tools/diagnostics.js';

afterEach(() => resetDiagnosticsForTest());

test('emitDiagnosticOnce is a no-op when no sink is configured', () => {
  resetDiagnosticsForTest();
  assert.doesNotThrow(() => emitDiagnosticOnce('no-sink', 'message'));
});

test('setDiagnosticSink routes diagnostics to the provided function', () => {
  const messages: string[] = [];
  setDiagnosticSink((message) => messages.push(message));
  emitDiagnosticOnce('routed', 'operator message');
  assert.deepEqual(messages, ['operator message']);
});

test('emitDiagnosticOnce emits once per key and allows a different key', () => {
  const messages: string[] = [];
  setDiagnosticSink((message) => messages.push(message));
  emitDiagnosticOnce('same', 'first');
  emitDiagnosticOnce('same', 'second');
  emitDiagnosticOnce('different', 'third');
  assert.deepEqual(messages, ['first', 'third']);
});

test('resetDiagnosticsForTest clears both emitted keys and the configured sink', () => {
  const messages: string[] = [];
  setDiagnosticSink((message) => messages.push(message));
  emitDiagnosticOnce('reset-key', 'before reset');

  resetDiagnosticsForTest();
  emitDiagnosticOnce('default-sink-key', 'dropped by default sink');
  setDiagnosticSink((message) => messages.push(message));
  emitDiagnosticOnce('reset-key', 'after reset');

  assert.deepEqual(messages, ['before reset', 'after reset']);
});
