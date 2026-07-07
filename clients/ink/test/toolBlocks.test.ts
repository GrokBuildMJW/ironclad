import {test} from 'node:test';
import assert from 'node:assert/strict';
import {splitToolBlocks, type Segment} from '../src/ui/toolBlocks.js';

// #1167 (epic #1144): split a committed turn body into ordered markdown + tool-call segments, so the client
// can render tool calls as a foldable component instead of raw `●`/`⎿` text.

test('#1167 plain markdown is a single md segment', () => {
  const segs = splitToolBlocks('## Heading\n\nsome text');
  assert.deepEqual(segs, [{type: 'md', text: '## Heading\n\nsome text'}] as Segment[]);
});

test('#1167 a tool call becomes a tool segment (label + result)', () => {
  const segs = splitToolBlocks('  ● Bash(ls -1)\n  ⎿ AGENTS.md\n     CLAUDE.md');
  assert.deepEqual(segs, [{type: 'tool', label: 'Bash(ls -1)', result: ['AGENTS.md', 'CLAUDE.md']}]);
});

test('#1167 markdown, tool, markdown keep their order', () => {
  const segs = splitToolBlocks('intro\n  ● Read(x.ts)\n  ⎿ 1 line\noutro');
  assert.deepEqual(segs, [
    {type: 'md', text: 'intro'},
    {type: 'tool', label: 'Read(x.ts)', result: ['1 line']},
    {type: 'md', text: 'outro'},
  ]);
});

test('#1167 a header with no result yields an empty result (tool still running)', () => {
  const segs = splitToolBlocks('  ● Bash(sleep 5)');
  assert.deepEqual(segs, [{type: 'tool', label: 'Bash(sleep 5)', result: []}]);
});

test('#1167 the ANSI colours the engine adds are stripped', () => {
  const segs = splitToolBlocks('\x1b[90m  ● Bash(ls)\x1b[0m\n\x1b[90m  ⎿ a.txt\x1b[0m');
  assert.deepEqual(segs, [{type: 'tool', label: 'Bash(ls)', result: ['a.txt']}]);
});

test('#1196 a coloured result line KEEPS its SGR; a grey-wrapped plain line stays stripped', () => {
  // #1196: the engine streams a coloured line (ls --color) WITHOUT the grey wrap, so its `⎿`/`     `
  // prefix is plain at the start and the raw capture keeps the inner SGR (the renderer paints it).
  const coloured = splitToolBlocks(
    '  ● Bash(ls -lA --color=always)\n  ⎿ \x1b[01;34md0\x1b[0m\n     \x1b[01;32mf0.txt\x1b[0m',
  );
  assert.deepEqual(coloured, [
    {type: 'tool', label: 'Bash(ls -lA --color=always)', result: ['\x1b[01;34md0\x1b[0m', '\x1b[01;32mf0.txt\x1b[0m']},
  ]);
  // a grey-wrapped plain line (the default styling for non-coloured output) is still stripped to plain
  const grey = splitToolBlocks('\x1b[90m  ● Bash(ls)\x1b[0m\n\x1b[90m  ⎿ a.txt\x1b[0m');
  assert.deepEqual(grey, [{type: 'tool', label: 'Bash(ls)', result: ['a.txt']}]);
});

test('#1167 the explicit overflow marker is kept in the result', () => {
  const segs = splitToolBlocks('  ● Bash(x)\n  ⎿ line0\n     … (+9 more lines)');
  assert.deepEqual(segs, [{type: 'tool', label: 'Bash(x)', result: ['line0', '… (+9 more lines)']}]);
});

test('#1167 two consecutive tool calls are two segments', () => {
  const segs = splitToolBlocks('  ● Read(a)\n  ⎿ a\n  ● Read(b)\n  ⎿ b');
  assert.deepEqual(segs, [
    {type: 'tool', label: 'Read(a)', result: ['a']},
    {type: 'tool', label: 'Read(b)', result: ['b']},
  ]);
});

test('#1167 a blank line inside a result (router-collapsed to "") stays in the result, not leaked to markdown', () => {
  // PowerShell output starts with a blank line; the router collapses the `     ` empty line to '' — which
  // must not end the tool block (else the rest of the listing leaks into a markdown segment).
  const segs = splitToolBlocks('  ● Bash(Get-ChildItem)\n  ⎿ \n\n     Mode\n     ----\n     d----- .claude');
  assert.equal(segs.length, 1, 'one tool segment — nothing leaked');
  assert.equal(segs[0]?.type, 'tool');
  assert.deepEqual((segs[0] as {result: string[]}).result, ['', '', 'Mode', '----', 'd----- .claude']);
});

test('#1196 native ANSI colours in a result are PRESERVED (a coloured listing shows its shell colours)', () => {
  // #1196 supersedes the earlier muted-palette choice for tool output: when a listing result carries its
  // own colour (ls --color / PowerShell), the engine streams it unwrapped and the client keeps the SGR so
  // the renderer paints the native colours. (The renderer sandboxes SGR — colour only, no cursor tricks.)
  const segs = splitToolBlocks(
    '  ● Bash(gci)\n  ⎿ \x1b[44;1md-----\x1b[0m .claude\n     \x1b[93m02:40\x1b[0m file',
  );
  assert.deepEqual((segs[0] as {result: string[]}).result, ['\x1b[44;1md-----\x1b[0m .claude', '\x1b[93m02:40\x1b[0m file']);
});
