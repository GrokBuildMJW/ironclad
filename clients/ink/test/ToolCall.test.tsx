import {test} from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToString} from '../src/render/ink-compat.js';
import {ToolCall, isErrorResult} from '../src/ui/ToolCall.js';

// #1167 (epic #1144): the foldable tool-call block renders collapsed by default (Claude-Code action summary
// + a one-line detail) and, when open, the exact `Kind(arg)` header + the full result under a `⎿` corner.
// Click-to-toggle is exercised in-app (dispatch onClick, covered by dispatch.test.ts).

test('#1167 collapsed shows the action summary + a one-line detail, hides the full result', () => {
  const {frame, unmount} = renderToString(<ToolCall label="Bash(ls -1)" result={['a.txt', 'b.txt']} />, 60, 6);
  const f = frame();
  assert.match(f, /● Ran 1 shell command/);
  assert.match(f, /⎿ \$ ls -1/);
  assert.doesNotMatch(f, /a\.txt/);
  unmount();
});

test('#1167 a still-running tool shows the present-progressive action', () => {
  const {frame, unmount} = renderToString(<ToolCall label="Bash(ls)" result={[]} />, 60, 6);
  assert.match(frame(), /● Running 1 shell command…/);
  unmount();
});

test('#1167 a done Read shows its line count', () => {
  const {frame, unmount} = renderToString(<ToolCall label="Read(x.ts)" result={['a', 'b', 'c']} />, 60, 6);
  assert.match(frame(), /● Read 3 lines/);
  unmount();
});

test('#1196 isError tests the STRIPPED text so a colour-at-column-0 error line still trips', () => {
  assert.equal(isErrorResult(['\x1b[31m✗ 2 tests failed\x1b[0m']), true);   // SGR before ✗ — must still trip
  assert.equal(isErrorResult(['\x1b[31mERROR: build failed\x1b[0m']), true);
  assert.equal(isErrorResult(['✗ plain fail']), true);                       // plain still works
  assert.equal(isErrorResult(['\x1b[01;34mdir0\x1b[0m', '\x1b[01;32mfile0\x1b[0m']), false); // a coloured listing is not an error
  assert.equal(isErrorResult(['all good']), false);
});

test('#1167 expanded shows the exact Kind(arg) header + the full result under a corner', () => {
  // a non-shell tool → the header is platform-independent (shell tools relabel by platform; see toolMeta test)
  const {frame, unmount} = renderToString(
    <ToolCall label="Read(x.ts)" result={['line a', 'line b']} defaultOpen />,
    60,
    8,
  );
  const f = frame();
  assert.match(f, /● Read\(x\.ts\)/);
  assert.match(f, /⎿ line a/);
  assert.match(f, /line b/);
  unmount();
});
