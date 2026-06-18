import test from 'node:test';
import assert from 'node:assert/strict';
import React, {useEffect} from 'react';
import {createRoot} from '../src/render/host.js';
import {createVNode} from '../src/render/vnode.js';
import {
  RenderContext,
  createRenderContext,
  useApp,
  useStdin,
  useStdout,
  useInput,
  emptyKey,
} from '../src/render/hooks.js';

const h = React.createElement;

function fakeStdout(): {stream: NodeJS.WriteStream; get: () => string} {
  let buf = '';
  const s = {write: (x: string): boolean => ((buf += x), true), columns: 80, rows: 24, isTTY: true};
  return {stream: s as unknown as NodeJS.WriteStream, get: () => buf};
}

function fakeStdin(): {stream: NodeJS.ReadStream; isRaw: () => boolean} {
  let raw = false;
  const s = {isTTY: true, setRawMode: (on: boolean): void => void (raw = on)};
  return {stream: s as unknown as NodeJS.ReadStream, isRaw: () => raw};
}

test('useInput subscribes, receives keys, and holds raw mode while active', () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const b = createRenderContext({stdout: out.stream, stdin: inp.stream, exit: () => {}});
  const received: Array<[string, boolean]> = [];

  function Comp(): React.ReactElement {
    useInput((input, key) => {
      received.push([input, key.return]);
    });
    return h('ink-text', null, 'x');
  }

  const root = createRoot(createVNode('ink-root'));
  root.render(h(RenderContext.Provider, {value: b.value}, h(Comp)));

  assert.equal(b.subscriberCount(), 1, 'subscribed after a flushSync render');
  assert.equal(inp.isRaw(), true, 'raw mode on while a useInput is active');

  b.emit('a', emptyKey());
  b.emit('', emptyKey({return: true}));
  assert.deepEqual(received, [['a', false], ['', true]]);

  root.unmount();
  assert.equal(b.subscriberCount(), 0, 'unsubscribed on unmount');
  assert.equal(inp.isRaw(), false, 'raw mode released on unmount');
});

test('useInput({isActive:false}) neither subscribes nor grabs raw mode', () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const b = createRenderContext({stdout: out.stream, stdin: inp.stream, exit: () => {}});

  function Comp(): React.ReactElement {
    useInput(() => {}, {isActive: false});
    return h('ink-text', null, 'x');
  }
  createRoot(createVNode('ink-root')).render(h(RenderContext.Provider, {value: b.value}, h(Comp)));

  assert.equal(b.subscriberCount(), 0);
  assert.equal(inp.isRaw(), false);
});

test('useApp().exit invokes the provided exit', () => {
  let exited = false;
  const out = fakeStdout();
  const b = createRenderContext({stdout: out.stream, exit: () => void (exited = true)});

  function Comp(): React.ReactElement {
    const {exit} = useApp();
    useEffect(() => exit(), [exit]);
    return h('ink-text', null, 'x');
  }
  createRoot(createVNode('ink-root')).render(h(RenderContext.Provider, {value: b.value}, h(Comp)));
  assert.equal(exited, true);
});

test('useStdout().write reaches the stream', () => {
  const out = fakeStdout();
  const b = createRenderContext({stdout: out.stream, exit: () => {}});

  function Comp(): React.ReactElement {
    const {write} = useStdout();
    useEffect(() => write('hi'), [write]);
    return h('ink-text', null, 'x');
  }
  createRoot(createVNode('ink-root')).render(h(RenderContext.Provider, {value: b.value}, h(Comp)));
  assert.equal(out.get(), 'hi');
});

test('useStdin() exposes the stream + raw-mode capability', () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const b = createRenderContext({stdout: out.stream, stdin: inp.stream, exit: () => {}});
  let supported: boolean | null = null;

  function Comp(): React.ReactElement {
    const {isRawModeSupported, stdin} = useStdin();
    useEffect(() => {
      supported = isRawModeSupported && stdin === inp.stream;
    }, [isRawModeSupported, stdin]);
    return h('ink-text', null, 'x');
  }
  createRoot(createVNode('ink-root')).render(h(RenderContext.Provider, {value: b.value}, h(Comp)));
  assert.equal(supported, true);
});

test('hooks throw when used outside the renderer context', () => {
  // The hook throws during render; our reconciler routes render errors to a boundary
  // (or onUncaughtError), not synchronously out of render() — so verify via a boundary.
  let caught: Error | null = null;
  class Boundary extends React.Component<{children: React.ReactNode}, {failed: boolean}> {
    state = {failed: false};
    static getDerivedStateFromError(): {failed: boolean} {
      return {failed: true};
    }
    componentDidCatch(error: Error): void {
      caught = error;
    }
    render(): React.ReactNode {
      return this.state.failed ? null : this.props.children;
    }
  }
  function Comp(): React.ReactElement {
    useApp();
    return h('ink-text', null, 'x');
  }

  const origError = console.error;
  console.error = (): void => {}; // silence React's expected caught-boundary log
  try {
    createRoot(createVNode('ink-root')).render(h(Boundary, null, h(Comp)));
  } finally {
    console.error = origError;
  }
  assert.ok(caught, 'an error was thrown during render');
  assert.match(String((caught as unknown as Error).message), /inside the renderer/);
});

test('createRenderContext derives isRawModeSupported from a TTY stdin', () => {
  const out = fakeStdout();
  const inp = fakeStdin();
  const withTty = createRenderContext({stdout: out.stream, stdin: inp.stream, exit: () => {}});
  assert.equal(withTty.value.isRawModeSupported, true);
  const noStdin = createRenderContext({stdout: out.stream, exit: () => {}});
  assert.equal(noStdin.value.isRawModeSupported, false);
});
