import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import React from 'react';
import {mount, stripAnsi} from '../src/render/ink-compat.js';
import {App} from '../src/ui/App.js';
import {Server} from '../src/net/server.js';

// End-to-end: the REAL App component, unchanged, mounted on our custom renderer with a fake
// terminal — proves it renders, takes keyboard input through our input bridge, and tears down
// cleanly. The server is unreachable (fetch resolves to an error note asynchronously).

function fakeStdout(columns = 80, rows = 24): {stream: NodeJS.WriteStream; text: () => string; clear: () => void} {
  const e = new EventEmitter();
  let buf = '';
  const stream = Object.assign(e, {columns, rows, write: (s: string) => ((buf += s), true)}) as unknown as NodeJS.WriteStream;
  return {stream, text: () => buf, clear: () => void (buf = '')};
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

test('App runs unchanged on the custom renderer: renders, takes input, tears down cleanly', () => {
  const out = fakeStdout(80, 24);
  const inp = fakeStdin();
  const srv = new Server('http://127.0.0.1:1', {timeoutMs: 300});
  const inst = mount(<App srv={srv} codedir="." maxAgents={3} />, {stdout: out.stream, stdin: inp.stream});

  // the banner drew on mount
  assert.match(stripAnsi(out.text()), /Ironclad/, 'header rendered');

  // type into the input buffer; each keystroke re-renders and the new char is emitted
  out.clear();
  inp.emitData('h');
  inp.emitData('i');
  const typed = stripAnsi(out.text());
  assert.ok(typed.includes('h') && typed.includes('i'), 'typed characters reached the buffer + redrew');

  inst.unmount();
  assert.ok(out.text().endsWith('\x1b[?1049l'), 'clean teardown — left the alternate screen last');
});

test('header shows "Ironclad CLI <version>" (not the URL); UAE mark sits in the corner', async () => {
  const out = fakeStdout(80, 24);
  const inp = fakeStdin();
  const srv = new Server('http://127.0.0.1:1', {timeoutMs: 200});
  const inst = mount(React.createElement(App, {srv, codedir: '.', maxAgents: 3}), {
    stdout: out.stream,
    stdin: inp.stream,
    altScreen: false,
  });
  await new Promise((r) => setTimeout(r, 50)); // let the init effect commit the header lines
  const f = stripAnsi(out.text());
  assert.ok(/Ironclad CLI \d/.test(f), 'versioned CLI line in the header');
  assert.ok(f.includes('Developed in the UAE'), 'UAE mark rendered (bottom-right corner)');
  assert.ok(!f.includes('127.0.0.1'), 'server URL not shown');
  inst.unmount();
});

test('Enter on an empty line is a no-op (does not crash the render loop)', () => {
  const out = fakeStdout(80, 24);
  const inp = fakeStdin();
  const srv = new Server('http://127.0.0.1:1', {timeoutMs: 300});
  const inst = mount(<App srv={srv} codedir="." maxAgents={3} />, {stdout: out.stream, stdin: inp.stream});
  inp.emitData('\r'); // submit empty → classify 'empty' → returns
  assert.match(stripAnsi(out.text()), /Ironclad/, 'still alive');
  inst.unmount();
});
