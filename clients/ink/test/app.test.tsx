import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {Box, Text, renderToString} from '../src/render/ink-compat.js';
import {App, committedBlock, committedContinuation, renderGuidedInput, renderStartupBanner, renderTurnBody} from '../src/ui/App.js';
import {Server} from '../src/net/server.js';
import {emptyKey} from '../src/render/hooks.js';

// Unreachable server → App still renders its header immediately (fetch resolves to the
// "unreachable" note asynchronously); this test only needs the initial frame. Rendered on OUR
// renderer (the client now mounts through ink-compat, not Stock Ink).
const srv = (): Server => new Server('http://127.0.0.1:1', {timeoutMs: 500});

function deferred<T>(): {promise: Promise<T>; resolve: (value: T) => void} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return {promise, resolve};
}

const renderTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

async function waitForFrame(frame: () => string, pattern: RegExp, timeoutMs = 2000): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const rendered = frame();
    if (pattern.test(rendered)) return rendered;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail(`frame did not match ${pattern} within ${timeoutMs}ms`);
}

test('App renders the Ironclad header + status footer on the custom renderer', async () => {
  const {frame, input, unmount} = renderToString(<App srv={srv()} codedir="." maxAgents={3} />, 100, 24);
  const f = frame();
  assert.match(f, /Ironclad/, 'brand shown');
  assert.match(f, /model/, 'status footer shown');
  input('/auto on', emptyKey());
  await renderTurn();
  input('', emptyKey({return: true}));
  await renderTurn();
  assert.match(frame(), /\[AUTO\] client poller ON .*≤3 local coders parallel/,
    'the local client limit is distinct from engine autopilot concurrency');
  unmount();
});

test('#1650 busy turn retries and renders the engine reason without an HttpError prefix', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (input) => {
    if (String(input).endsWith('/chat/stream')) {
      return new Response(JSON.stringify({
        ok: false,
        error: 'busy — another turn is still running; try again shortly',
      }), {status: 503, headers: {'content-type': 'application/json'}});
    }
    return new Response('{}', {status: 200});
  }) as typeof fetch;
  const {frame, input, unmount} = renderToString(
    <App srv={new Server('http://h:8100')} codedir="." maxAgents={3} />,
    120,
    40,
  );
  try {
    input('trigger busy turn', emptyKey());
    await renderTurn();
    input('', emptyKey({return: true}));
    const rendered = await waitForFrame(frame, /✗ busy — another turn is still running/);
    assert.match(rendered, /↻ busy — another turn is still running; try again shortly — retrying \(2\/3\)/);
    assert.doesNotMatch(rendered, /HttpError/);
  } finally {
    globalThis.fetch = realFetch;
    unmount();
  }
});

test('#1621 block starts retain one top-spaced row', () => {
  const cases = [
    committedBlock(<Text>local</Text>),
    committedBlock(<Text>! shell</Text>),
    committedBlock(renderTurnBody('turn', 60)),
  ];
  for (const block of cases) {
    const {frame, unmount} = renderToString(
      <Box flexDirection="column"><Text>previous</Text>{block}</Box>,
      80,
      8,
    );
    const lines = frame().split('\n');
    assert.equal(lines[0], 'previous');
    assert.equal(lines[1], '', 'exactly one blank row above the committed block');
    assert.match(lines[2] ?? '', /local|! shell|turn/);
    assert.notEqual(lines[3], '', 'no second top-margin row');
    unmount();
  }
});

test('#1645 a continuation is tight under its block-start without removing the block boundary', () => {
  const {frame, unmount} = renderToString(
    <Box flexDirection="column">
      <Text>previous</Text>
      {committedBlock(<Text>block start</Text>)}
      {committedContinuation(<Text>live continuation</Text>)}
    </Box>,
    80,
    8,
  );
  assert.deepEqual(frame().split('\n').slice(0, 4), ['previous', '', 'block start', 'live continuation']);
  unmount();
});

test('#1645 startup banner is one tight block separated from adjacent blocks', () => {
  const {frame, unmount} = renderToString(
    <Box flexDirection="column">
      <Text>previous</Text>
      {committedBlock(renderStartupBanner('.', 3))}
      {committedBlock(<Text>next block</Text>)}
    </Box>,
    100,
    10,
  );
  const lines = frame().split('\n');
  const title = lines.findIndex((line) => line.includes('Ironclad · Orchestrator Client'));
  const next = lines.indexOf('next block');
  assert.equal(lines[title - 1], '', 'banner remains separated from the previous block');
  assert.match(lines[title + 1] ?? '', /Ironclad CLI 0\.1\.0 · code \. · ≤3 agents/);
  assert.equal(lines[title + 2], ' /help · exit', 'no blank rows inside the banner');
  assert.equal(lines[next - 1], '', 'the following block keeps its own boundary');
  assert.equal(next, title + 4, 'exactly one blank row separates the following block');
  unmount();
});

test('#1645 guided input lines stay tight inside one committed block', () => {
  const guide = renderGuidedInput({
    command: 'config set',
    usage: '/config set <dotted.key> <value>',
    subcommands: ['show', 'set'],
    fields: [{name: '<dotted.key>', required: true, choices: [], default: '', type: 'string'}],
    canonical_echo: '/config set',
  });
  const {frame, unmount} = renderToString(
    <Box flexDirection="column"><Text>previous</Text>{committedBlock(guide)}</Box>,
    100,
    8,
  );
  const lines = frame().split('\n');
  const header = lines.findIndex((line) => line.includes('guided input for /config set:'));
  assert.equal(lines[header - 1], '', 'guided output is separated from the previous block');
  assert.match(lines[header + 1] ?? '', /usage: \/config set/);
  assert.match(lines[header + 2] ?? '', /subcommands: show \| set/);
  assert.match(lines[header + 3] ?? '', /<dotted\.key>  \(required\)/);
  unmount();
});

test('#1645 /sh renders its echo live as a block-start and its output as a tight continuation', async () => {
  const shell = deferred<string>();
  const commands: string[] = [];
  const operatorShell = (command: string): Promise<string> => {
    commands.push(command);
    return shell.promise;
  };
  const {frame, input, unmount} = renderToString(
    <App srv={srv()} codedir="." maxAgents={3} operatorShell={operatorShell} />,
    120,
    40,
  );
  try {
    const payload = '! deterministic-shell-command';
    input(payload, emptyKey());
    await renderTurn();
    input('', emptyKey({return: true}));
    await renderTurn();
    assert.deepEqual(commands, ['deterministic-shell-command'], 'the injected shell call is awaiting its result');

    let lines = frame().split('\n');
    const echo = lines.findIndex((line) => line.includes(payload));
    assert.ok(echo > 0, 'the shell echo renders while the injected command is pending');
    assert.equal(lines[echo - 1], '', 'the echo retains its block-start margin');
    assert.equal(lines.some((line) => line.trim() === 'shell-live-output'), false, 'output is still pending');

    shell.resolve('shell-live-output');
    await renderTurn();

    lines = frame().split('\n');
    assert.equal(lines[echo + 1], '  shell-live-output', 'shell output is tight under the live echo');
  } finally {
    unmount();
  }
});

test('#1645 an awaited continuation starts a new block after a foreign commit', async () => {
  const shell = deferred<string>();
  const commands: string[] = [];
  const operatorShell = (command: string): Promise<string> => {
    commands.push(command);
    return shell.promise;
  };
  const {frame, input, unmount} = renderToString(
    <App srv={srv()} codedir="." maxAgents={3} operatorShell={operatorShell} />,
    120,
    60,
  );
  try {
    const payload = '! deterministic-shell-command';
    input(payload, emptyKey());
    await renderTurn();
    input('', emptyKey({return: true}));
    await renderTurn();
    assert.deepEqual(commands, ['deterministic-shell-command'], 'the injected shell call is awaiting its result');

    input('/help', emptyKey());
    await renderTurn();
    input('', emptyKey({return: true}));
    await renderTurn();

    let lines = frame().split('\n');
    const echo = lines.findIndex((line) => line.includes(payload));
    const foreign = lines.findIndex((line) => line.includes('Commands (with a / prefix)'));
    assert.ok(echo >= 0 && foreign > echo, 'the foreign help block lands during the shell await');
    assert.equal(lines.some((line) => line.trim() === 'delayed-shell-output'), false, 'shell output is still pending');

    shell.resolve('delayed-shell-output');
    await renderTurn();

    lines = frame().split('\n');
    const output = lines.findIndex((line) => line.trim() === 'delayed-shell-output');
    assert.ok(output > foreign, 'the awaited shell output lands after the foreign help block');
    assert.equal(lines[output - 1], '', 'the shell continuation regains its own block separation after the foreign commit');
  } finally {
    unmount();
  }
});
