import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {runTool} from '../src/tools/runTool.js';

async function tmpWith(n: number): Promise<string> {
  const d = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-list-'));
  for (let i = 0; i < n; i++) {
    await fs.writeFile(path.join(d, `f${String(i).padStart(4, '0')}.txt`), 'x');
  }
  return d;
}

test('limit branch: suffix "(limit=N)" and the GX10v3 note appears (limit-then-cap order)', async () => {
  const d = await tmpWith(5);
  const out = await runTool('list_directory', {path: d, limit: 2});
  const lines = out.split('\n');
  assert.equal(lines.length, 3, '2 entries + 1 note line');
  assert.match(out, /\.\.\. \[GX10v3: showing 2 of 5 entries \(limit=2\)\]$/);
  await fs.rm(d, {recursive: true, force: true});
});

test('hard-cap branch: >200 entries → "(hard cap 200 — use sort=\'time\'+limit)"', async () => {
  const d = await tmpWith(205);
  const out = await runTool('list_directory', {path: d});
  const lines = out.split('\n');
  assert.equal(lines.length, 201, '200 entries + 1 note line');
  assert.match(out, /\.\.\. \[GX10v3: showing 200 of 205 entries \(hard cap 200 — use sort='time'\+limit\)\]$/);
  await fs.rm(d, {recursive: true, force: true});
});

test('no trim → no note (shown == total)', async () => {
  const d = await tmpWith(3);
  const out = await runTool('list_directory', {path: d});
  assert.doesNotMatch(out, /GX10v3/);
  assert.equal(out.split('\n').length, 3);
  await fs.rm(d, {recursive: true, force: true});
});

test('list_directory missing → ERROR: Not found with str(Path) form', async () => {
  const out = await runTool('list_directory', {path: 'definitely/missing/dir'});
  assert.equal(out, `ERROR: Not found: ${['definitely', 'missing', 'dir'].join(path.sep)}`);
});
