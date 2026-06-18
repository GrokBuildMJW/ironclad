/**
 * Spike C (GATE) — the ink 7 dependency surface the later phases rely on is present on
 * THIS runtime (Node 24): usePaste (bracketed paste), useInput (editor), useWindowSize
 * (resize), useCursor (caret), useStdin/useStdout, render + Static. maxFps is proven a
 * valid RenderOptions key at type level (registry-verified default 30; we opt into 60).
 * Bun-runtime variant deferred (we ship on Node 24 per the plan fallback).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import * as ink from 'ink';
import type {RenderOptions} from 'ink';

// Compile-time proof that maxFps is a real render option.
const _maxFpsProof: Pick<RenderOptions, 'maxFps'> = {maxFps: 60};
void _maxFpsProof;

test('Spike C — ink 7 exposes the dependency surface (Node 24)', () => {
  const m = ink as unknown as Record<string, unknown>;
  for (const name of [
    'usePaste',
    'useInput',
    'useWindowSize',
    'useCursor',
    'useStdin',
    'useStdout',
    'render',
    'Static',
  ]) {
    assert.equal(typeof m[name], 'function', `ink.${name} must be a function (have ${typeof m[name]})`);
  }
});
