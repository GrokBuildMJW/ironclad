import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToString} from '../src/render/ink-compat.js';
import {Footer} from '../src/ui/Footer.js';
import {WorkingLine} from '../src/ui/WorkingLine.js';
import {InputBox} from '../src/ui/InputBox.js';
import type {StatusState} from '../src/ui/useStatusPoller.js';

// Rendered on OUR renderer via ink-compat (the components are unchanged; only their Box/Text/hooks
// now resolve to the custom renderer instead of Stock Ink).

test('Footer — model · conn · tasks · perf', () => {
  const st: StatusState = {
    model: 'qwen3.6-35b',
    connected: true,
    watcher: true,
    autopilot: false,
    pending: 1,
    inProgress: 0,
    done: 4,
    perf: 'TTFT 0.5s · 62 tok/s',
  };
  const {frame, unmount} = renderToString(<Footer st={st} />, 100, 3);
  const f = frame();
  assert.match(f, /Ironclad/);
  assert.match(f, /qwen3\.6-35b/);
  assert.match(f, /conn/);
  assert.match(f, /1P\/0IP\/4D/);
  assert.match(f, /TTFT 0\.5s/);
  unmount();
});

test('WorkingLine — verb + elapsed + tokens + interrupt hint', () => {
  const {frame, unmount} = renderToString(<WorkingLine verb="Pondering" frame={4} seconds={3} tokens={1500} />, 80, 3);
  const f = frame();
  assert.match(f, /Pondering…/);
  assert.match(f, /3s/);
  assert.match(f, /1\.5k tokens/);
  assert.match(f, /esc to interrupt/);
  unmount();
});

test('InputBox — ruled prompt with buffer + caret', () => {
  const {frame, unmount} = renderToString(<InputBox buffer="hello world" caret hint="hint" />, 80, 4);
  const f = frame();
  assert.match(f, /> hello world/, 'prompt + buffer');
  assert.match(f, /─/, 'has a rule line');
  unmount();
});

test('InputBox — empty shows hint', () => {
  const {frame, unmount} = renderToString(<InputBox buffer="" caret={false} hint="Frag etwas …" />, 80, 4);
  assert.match(frame(), /Frag etwas …/);
  unmount();
});
