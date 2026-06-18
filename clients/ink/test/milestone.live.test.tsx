/**
 * Phase 1 milestone (LIVE) — type a turn into the App, it streams from the Spark, the
 * answer commits, and the footer perf populates. Gated on GX10_LIVE_URL.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {render} from 'ink-testing-library';
import {App} from '../src/ui/App.js';
import {Server} from '../src/net/server.js';

const LIVE = process.env['GX10_LIVE_URL'];
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

test('milestone — App shows live model in the footer', {skip: !LIVE}, async () => {
  const srv = new Server(LIVE as string);
  const {lastFrame, unmount} = render(<App srv={srv} codedir="." maxAgents={3} />);
  for (let i = 0; i < 50 && /model —/.test(lastFrame() ?? ''); i++) await sleep(100);
  assert.doesNotMatch(lastFrame() ?? '', /model —/, 'footer model populated from /health');
  unmount();
});

test('milestone — typing a turn streams it; footer perf populates', {skip: !LIVE}, async () => {
  const srv = new Server(LIVE as string);
  const {lastFrame, stdin, unmount} = render(<App srv={srv} codedir="." maxAgents={3} />);
  await sleep(400);
  // short, deterministic prompt — NOT "wer bist du?" (known thinking-runaway, see memory note);
  // we only assert that a real turn streams end-to-end and perf lands in the footer.
  stdin.write('Antworte knapp mit einem Wort: bereit');
  await sleep(150); // let React commit setBuffer before Enter (in real use these are separate stdin events)
  stdin.write('\r'); // Enter → submit
  // whitespace-insensitive: Yoga wraps the footer at the test width and can split "tok/s"/"TTFT"
  // across a line break, so strip all whitespace before matching the perf marker.
  const perfSeen = (): boolean => /TTFT|tok\//.test((lastFrame() ?? '').replace(/\s/g, ''));
  for (let i = 0; i < 450 && !perfSeen(); i++) await sleep(100);
  const frame = lastFrame() ?? '';
  assert.ok(perfSeen(), `footer perf populated → a turn streamed end-to-end. frame: ${frame.slice(-240)}`);
  unmount();
});
