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
  assert.equal(lines.length, 4, '#1183 count header + 2 entries + 1 note line');
  assert.match(out, /\.\.\. \[GX10v3: showing 2 of 5 entries \(limit=2\)\]$/);
  await fs.rm(d, {recursive: true, force: true});
});

test('hard-cap branch: >200 entries stops at cap-plus-one and reports many', async () => {
  const d = await tmpWith(205);
  const out = await runTool('list_directory', {path: d});
  const lines = out.split('\n');
  assert.equal(lines.length, 202, '#1183 count header + 200 entries + 1 note line');
  assert.match(out, /^At least 0 directories, 201 files\n/);
  assert.match(out, /\.\.\. \[GX10v3: first 200 entries \(filesystem order\) of many; hard cap 200 — narrow the path for a complete listing; a sort\/limit ranks only this partial sample, not the whole directory\]$/);
  await fs.rm(d, {recursive: true, force: true});
});

test('no trim → no note (shown == total)', async () => {
  const d = await tmpWith(3);
  const out = await runTool('list_directory', {path: d});
  assert.doesNotMatch(out, /GX10v3/);
  assert.match(out, /^0 directories, 3 files\n/, '#1183 deterministic count header of the full set');
  assert.equal(out.split('\n').length, 4);
  await fs.rm(d, {recursive: true, force: true});
});

test('list_directory missing → ERROR: Not found with str(Path) form', async () => {
  const out = await runTool('list_directory', {path: 'definitely/missing/dir'});
  assert.equal(out, `ERROR: Not found: ${['definitely', 'missing', 'dir'].join(path.sep)}`);
});
