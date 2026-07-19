import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToString} from '../src/render/ink-compat.js';
import {Footer} from '../src/ui/Footer.js';
import {WorkingLine} from '../src/ui/WorkingLine.js';
import {InputBox} from '../src/ui/InputBox.js';
import {CommandMenu} from '../src/ui/CommandMenu.js';
import {completions} from '../src/commands.js';
import type {StatusState} from '../src/ui/useStatusPoller.js';

// Rendered on OUR renderer via ink-compat (the components are unchanged; only their Box/Text/hooks
// now resolve to the custom renderer instead of Stock Ink).

test('Footer — model · conn · mem · tasks · perf', () => {
  const st: StatusState = {
    model: 'qwen3.6-35b',
    connected: true,
    memory: 'up',
    warm: 'up',
    watcher: true,
    autopilot: false,
    pending: 1,
    inProgress: 0,
    done: 4,
    perf: 'TTFT 0.5s · 62 tok/s',
    agent: 'codex · cheapest-capable',
    search: 'n=2 ms=153',
  };
  const {frame, unmount} = renderToString(<Footer st={st} />, 200, 3);
  const f = frame();
  assert.match(f, /Ironclad/);
  assert.match(f, /qwen3\.6-35b/);
  assert.match(f, /conn/);
  assert.match(f, /mem up/, 'shows memory status'); // MEM-7
  assert.match(f, /warm up/, '#385: shows the Warm (Valkey) tier separately');
  assert.match(f, /1P\/0IP\/4D/);
  assert.match(f, /TTFT 0\.5s/);
  assert.match(f, /coder codex/, '#453: shows which coder was routed');
  assert.match(f, /web 2 · 153ms/, '#505 S9: shows the web-search summary chip');
  unmount();
});

test('Footer — memory off/down render their state', () => {
  const base: StatusState = {
    model: 'm', connected: true, memory: 'off', warm: 'off', watcher: false, autopilot: false,
    pending: 0, inProgress: 0, done: 0, perf: '', agent: '', search: '',
  };
  const off = renderToString(<Footer st={base} />, 120, 3);
  assert.match(off.frame(), /mem off/);
  assert.match(off.frame(), /warm off/);                 // #385: warm tri-state rendered too
  off.unmount();
  const down = renderToString(<Footer st={{...base, memory: 'down', warm: 'down'}} />, 120, 3);
  assert.match(down.frame(), /mem down/);
  assert.match(down.frame(), /warm down/);
  down.unmount();
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

test('InputBox — empty shows English default hint', () => {
  const {frame, unmount} = renderToString(<InputBox buffer="" caret={false} />, 80, 4);
  assert.match(frame(), /Ask something …/);
  unmount();
});

test('CommandMenu — lists matches and marks the selected row (MEM-16(2))', () => {
  const items = completions('re'); // [reset, resume]
  const {frame, unmount} = renderToString(<CommandMenu items={items} sel={1} />, 100, 6);
  const f = frame();
  assert.match(f, /\/reset/);
  assert.match(f, /\/resume/);
  assert.match(f, /›/, 'has a selection marker');
  assert.match(f, /Tab complete/, 'shows the key hint');
  unmount();
});

test('CommandMenu — empty list renders nothing', () => {
  const {frame, unmount} = renderToString(<CommandMenu items={[]} sel={0} />, 100, 3);
  assert.equal(frame().trim(), '');
  unmount();
});
