import {test} from 'node:test';
import assert from 'node:assert/strict';
import {toolMeta} from '../src/ui/toolMeta.js';

// #1167 step 2 (epic #1144): the engine's `Kind(arg)` label → Claude-Code phrasing (action summary, a
// one-line detail, and the exact header shown expanded).

test('#1167 Bash: present-progressive while running, past-tense done, `$ cmd` detail', () => {
  assert.equal(toolMeta('Bash(ls -1)', false, 0).summary, 'Running 1 shell command…');
  const done = toolMeta('Bash(ls -1)', true, 5);
  assert.equal(done.summary, 'Ran 1 shell command');
  assert.equal(done.detail, '$ ls -1');
  assert.equal(done.header, 'Bash(ls -1)');
});

test('#1167 Read uses a line count when done', () => {
  assert.equal(toolMeta('Read(x.ts)', false, 0).summary, 'Reading 1 file…');
  assert.equal(toolMeta('Read(x.ts)', true, 70).summary, 'Read 70 lines');
  assert.equal(toolMeta('Read(x.ts)', true, 1).summary, 'Read 1 line');
  assert.equal(toolMeta('Read(x.ts)', true, 70).detail, 'x.ts');
});

test('#1167 Write / List / Issue map to their verbs', () => {
  assert.equal(toolMeta('Write(a.md)', false, 0).summary, 'Writing 1 file…');
  assert.equal(toolMeta('Write(a.md)', true, 1).summary, 'Wrote 1 file');
  assert.equal(toolMeta('List(.)', true, 3).summary, 'Listed 1 directory');
  assert.equal(toolMeta('Issue(fix x)', true, 1).summary, 'Created 1 issue');
});

test('#1167 an unknown tool falls back to its bare label', () => {
  assert.equal(toolMeta('advance_pipeline(T-1)', false, 0).summary, 'advance_pipeline(T-1)…');
  assert.equal(toolMeta('advance_pipeline(T-1)', true, 1).summary, 'advance_pipeline(T-1)');
  assert.equal(toolMeta('advance_pipeline(T-1)', true, 1).detail, 'T-1');
});

test('#1167 step 3: a shell command uses the actual shell name in the expanded header', () => {
  assert.equal(toolMeta('Bash(ls -1)', true, 3, 'PowerShell').header, 'PowerShell(ls -1)'); // Windows
  assert.equal(toolMeta('Bash(ls -1)', true, 3, 'Bash').header, 'Bash(ls -1)'); // POSIX
  assert.equal(toolMeta('Bash(ls -1)', true, 3).header, 'Bash(ls -1)'); // default
  assert.equal(toolMeta('Bash(ls -1)', true, 3, 'PowerShell').summary, 'Ran 1 shell command'); // summary stays generic
});
