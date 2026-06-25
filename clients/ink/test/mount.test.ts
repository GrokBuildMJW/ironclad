import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import React from 'react';
import {mount} from '../src/render/mount.js';
import {useInput} from '../src/render/hooks.js';

const h = React.createElement;

function fakeStdout(columns = 40, rows = 10): {
  stream: NodeJS.WriteStream;
  text: () => string;
  clear: () => void;
  resize: (c: number, r: number) => void;
} {
  const e = new EventEmitter();
  let buf = '';
  const stream = Object.assign(e, {columns, rows, write: (s: string) => ((buf += s), true)}) as unknown as NodeJS.WriteStream;
  return {
    stream,
    text: () => buf,
    clear: () => {
      buf = '';
    },
    resize: (c: number, r: number) => {
      (stream as unknown as {columns: number}).columns = c;
      (stream as unknown as {rows: number}).rows = r;
      e.emit('resize');
    },
  };
}

function fakeStdin(): {stream: NodeJS.ReadStream; emitData: (d: string) => void} {
  const e = new EventEmitter();
  const stream = Object.assign(e, {
    isTTY: true,
    setRawMode: () => {},
    resume: () => {},
    pause: () => {},
    setEncoding: () => {},
  }) as unknown as NodeJS.ReadStream;
  return {stream, emitData: (d: string) => void e.emit('data', d)};
}

test('mount renders a component to the terminal', () => {
  const out = fakeStdout();
  const inst = mount(h('ink-text', null, 'hello'), {stdout: out.stream, altScreen: false});
  assert.ok(out.text().includes('hello'), 'text drawn');
  inst.unmount();
});

test('rerender updates the output', () => {
  const out = fakeStdout();
  const inst = mount(h('ink-text', null, 'one'), {stdout: out.stream, altScreen: false});
  out.clear();
  inst.rerender(h('ink-text', null, 'two'));
  assert.ok(out.text().includes('two'), 'updated text drawn');
  inst.unmount();
});

test('clean teardown leaves the alternate screen and disables mouse', () => {
  const out = fakeStdout();
  const inst = mount(h('ink-text', null, 'x'), {stdout: out.stream, altScreen: true});
  assert.ok(out.text().includes('\x1b[?1049h'), 'entered alt screen');
  out.clear();
  inst.unmount();
  const tail = out.text();
  assert.ok(tail.includes('\x1b[?1000l'), 'mouse disabled');
  assert.ok(tail.includes('\x1b[?25h'), 'cursor restored');
  assert.ok(tail.endsWith('\x1b[?1049l'), 'left alt screen last');
});

test('useInput receives keys fed through stdin', () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const received: string[] = [];
  function App(): React.ReactElement {
    useInput((input) => received.push(input));
    return h('ink-text', null, 'x');
  }
  const inst = mount(h(App), {stdout: out.stream, stdin: inp.stream, altScreen: false});
  inp.emitData('a');
  inp.emitData('b');
  assert.deepEqual(received, ['a', 'b']);
  inst.unmount();
});

test('bracketed paste injects the content as one input (markers stripped)', () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const received: string[] = [];
  function App(): React.ReactElement {
    useInput((input) => received.push(input));
    return h('ink-text', null, 'x');
  }
  const inst = mount(h(App), {stdout: out.stream, stdin: inp.stream, altScreen: false});
  inp.emitData('\x1b[200~hello world\x1b[201~');
  assert.deepEqual(received, ['hello world']);
  inst.unmount();
});

test('bracketed paste carries key.paste=true so the app can collapse it (#438)', () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const flags: boolean[] = [];
  const typed: boolean[] = [];
  function App(): React.ReactElement {
    useInput((_input, key) => {flags.push(key.paste); typed.push(false);});
    return h('ink-text', null, 'x');
  }
  const inst = mount(h(App), {stdout: out.stream, stdin: inp.stream, altScreen: false});
  inp.emitData('\x1b[200~line one\nline two\x1b[201~'); // a multi-line paste
  inp.emitData('a');                                     // a typed key
  assert.deepEqual(flags, [true, false]); // paste flagged; typed input is not
  inst.unmount();
});

test('right-click pastes the OS clipboard into the input', async () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const received: string[] = [];
  function App(): React.ReactElement {
    useInput((input) => received.push(input));
    return h('ink-text', null, 'x');
  }
  const inst = mount(h(App), {
    stdout: out.stream,
    stdin: inp.stream,
    altScreen: false,
    readClipboard: (cb) => queueMicrotask(() => cb('pasted!')), // the real reader is async
  });
  inp.emitData('\x1b[<2;3;1M'); // right-button down (SGR mouse, button 2)
  await Promise.resolve(); // let the async clipboard read deliver
  assert.deepEqual(received, ['pasted!']);
  inst.unmount();
});

test('Ctrl+C copies an active selection instead of exiting', () => {
  const out = fakeStdout(20, 5);
  const inp = fakeStdin();
  const inst = mount(h('ink-text', null, 'hello'), {stdout: out.stream, stdin: inp.stream, nativeClipboard: false});
  // drag-select "hello" (SGR mouse: down at col1, drag to col5, release)
  inp.emitData('\x1b[<0;1;1M');
  inp.emitData('\x1b[<32;5;1M');
  inp.emitData('\x1b[<0;5;1m');
  out.clear();
  inp.emitData('\x03'); // Ctrl+C
  assert.ok(out.text().includes('\x1b]52;c;'), 'selection copied via OSC 52');
  assert.ok(!out.text().includes('\x1b[?1049l'), 'did NOT tear down the session');
  inst.unmount();
});

test('Ctrl+C reaches a live input handler instead of force-exiting the session', () => {
  const out = fakeStdout(20, 5);
  const inp = fakeStdin();
  const seen: Array<{input: string; ctrl: boolean}> = [];
  function App(): React.ReactElement {
    useInput((input, key) => seen.push({input, ctrl: key.ctrl}));
    return h('ink-text', null, 'busy');
  }
  const inst = mount(h(App), {stdout: out.stream, stdin: inp.stream, altScreen: true});
  out.clear();
  inp.emitData('\x03'); // Ctrl+C, no selection, with a useInput subscriber listening
  assert.ok(
    seen.some((e) => e.ctrl && e.input === 'c'),
    'Ctrl+C is delivered to useInput (so the app can cancel a turn)',
  );
  assert.ok(!out.text().includes('\x1b[?1049l'), 'did NOT tear down the session');
  inst.unmount();
});

test('Ctrl+C exits and resolves waitUntilExit', async () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const inst = mount(h('ink-text', null, 'x'), {stdout: out.stream, stdin: inp.stream, altScreen: true});
  inp.emitData('\x03');
  await inst.waitUntilExit(); // resolves on the Ctrl+C unmount
  assert.ok(out.text().endsWith('\x1b[?1049l'), 'torn down cleanly on Ctrl+C');
});

test('content taller than the screen anchors to the bottom (input/footer stay visible)', () => {
  const out = fakeStdout(20, 4); // 4 rows
  const lines = Array.from({length: 8}, (_, i) => h('ink-text', null, 'line' + i));
  const inst = mount(h('ink-box', {flexDirection: 'column'}, ...lines), {stdout: out.stream, altScreen: false});
  const txt = out.text();
  assert.ok(txt.includes('line7'), 'the bottom line stays visible');
  assert.ok(!txt.includes('line0'), 'the top scrolled off (not the bottom clipped)');
  inst.unmount();
});

test('Ctrl+F opens search; typing finds matches and shows the count', () => {
  const out = fakeStdout(30, 6);
  const inp = fakeStdin();
  const tree = h(
    'ink-box',
    {flexDirection: 'column'},
    h('ink-text', null, 'a needle here'),
    h('ink-text', null, 'plus needle two'),
  );
  const inst = mount(tree, {stdout: out.stream, stdin: inp.stream, altScreen: false});
  out.clear();
  inp.emitData('\x06'); // Ctrl+F
  // the diff emits only changed cells, so the bar builds up incrementally across frames — assert on
  // the stable tokens it writes (the opened bar's "0/0", then the live match counter "1/2").
  const opened = out.text();
  assert.ok(opened.includes('0/0'), 'search bar opened (empty query)');
  out.clear();
  for (const ch of 'needle') inp.emitData(ch);
  assert.ok(out.text().includes('1/2'), 'two matches found, current is the first');
  inp.emitData('\x1b'); // Esc closes without crashing
  inst.unmount();
});

test('shows the hardware cursor at the declared caret cell', () => {
  const out = fakeStdout(20, 4);
  const inst = mount(h('ink-text', {cursor: true}, '> hi'), {stdout: out.stream, altScreen: false});
  assert.match(out.text(), /\x1b\[1;5H/, 'cursor positioned at row 1, col 5 (just after "> hi")');
  assert.ok(out.text().includes('\x1b[?25h'), 'cursor shown');
  inst.unmount();
});

test('hides the hardware cursor when nothing declares one', () => {
  const out = fakeStdout(20, 4);
  const inst = mount(h('ink-text', null, 'plain'), {stdout: out.stream, altScreen: false});
  assert.ok(out.text().includes('\x1b[?25l'), 'cursor hidden when no caret is declared');
  inst.unmount();
});

test('resize clears the screen and repaints (ghost-free)', () => {
  const out = fakeStdout(40, 10);
  const inst = mount(h('ink-text', null, 'hi'), {stdout: out.stream, altScreen: true});
  out.clear();
  out.resize(60, 20);
  assert.ok(out.text().includes('\x1b[2J'), 'screen cleared on resize');
  assert.ok(out.text().includes('hi'), 'repainted after resize');
  inst.unmount();
});

test('altScreen:false emits no alternate-screen sequences', () => {
  const out = fakeStdout();
  const inst = mount(h('ink-text', null, 'x'), {stdout: out.stream, altScreen: false});
  inst.unmount();
  assert.ok(!out.text().includes('1049'), 'no alt-screen switch');
});
