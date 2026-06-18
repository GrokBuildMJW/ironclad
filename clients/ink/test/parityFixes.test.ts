/** Tests proving the audit-derived parity fixes (glob order, move/copy-into-dir, int bool,
 *  splitlines exotic separators, required-arg guard). */
import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {runTool} from '../src/tools/runTool.js';

async function tmp(): Promise<string> {
  return fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-parity-'));
}
async function inDir(dir: string, body: () => Promise<void>): Promise<void> {
  const prev = process.cwd();
  process.chdir(dir);
  try {
    await body();
  } finally {
    process.chdir(prev);
  }
}

test('search_files: ALL current-dir matches precede subdir matches (rglob order)', async () => {
  const d = await tmp();
  // "a_sub" sorts before "z.md": the old interleaved walk would emit a_sub/inner.md first.
  await fs.mkdir(path.join(d, 'a_sub'));
  await fs.writeFile(path.join(d, 'a_sub', 'inner.md'), 'needle here');
  await fs.writeFile(path.join(d, 'z.md'), 'needle here');
  await inDir(d, async () => {
    const out = await runTool('search_files', {pattern: 'needle', directory: '.', file_pattern: '*.md'});
    const iCur = out.indexOf('z.md:');
    const iSub = out.indexOf(`a_sub${path.sep}inner.md:`);
    assert.ok(iCur !== -1 && iSub !== -1, `both hits present:\n${out}`);
    assert.ok(iCur < iSub, `current-dir hit must precede subdir hit:\n${out}`);
  });
  await fs.rm(d, {recursive: true, force: true});
});

test('move_file into an existing directory moves INTO it (shutil.move parity)', async () => {
  const d = await tmp();
  const src = path.join(d, 'item.txt');
  const into = path.join(d, 'dest');
  await fs.writeFile(src, 'x');
  await fs.mkdir(into);
  const r = await runTool('move_file', {source: src, destination: into});
  assert.equal(r, `OK: Moved ${src} → ${into}`);
  assert.equal(await runTool('read_file', {path: path.join(into, 'item.txt')}), 'x', 'landed inside dest/');
  await fs.rm(d, {recursive: true, force: true});
});

test('copy_file into an existing directory copies INTO it (shutil.copy2 parity)', async () => {
  const d = await tmp();
  const src = path.join(d, 'item.txt');
  const into = path.join(d, 'dest');
  await fs.writeFile(src, 'y');
  await fs.mkdir(into);
  await runTool('copy_file', {source: src, destination: into});
  assert.equal(await runTool('read_file', {path: path.join(into, 'item.txt')}), 'y', 'copied inside dest/');
  await fs.rm(d, {recursive: true, force: true});
});

test('list_directory limit=true → 1 item (int(True)=1) with (limit=1) note', async () => {
  const d = await tmp();
  for (const n of ['a', 'b', 'c']) await fs.writeFile(path.join(d, `${n}.txt`), '1');
  const out = await runTool('list_directory', {path: d, limit: true});
  assert.match(out, /\(limit=1\)\]$/);
  assert.equal(out.split('\n').length, 2, '1 entry + note');
  await fs.rm(d, {recursive: true, force: true});
});

test('search_files splits on form-feed (\\f) like Python splitlines', async () => {
  const d = await tmp();
  await fs.writeFile(path.join(d, 'a.md'), 'alpha\fbeta-needle'); // \f = U+000C page break
  await inDir(d, async () => {
    const out = await runTool('search_files', {pattern: 'needle', directory: '.'});
    assert.match(out, /a\.md:2: beta-needle/, 'form-feed counted as a line boundary → line 2');
  });
  await fs.rm(d, {recursive: true, force: true});
});

test('missing required arg → ERROR (no operating on "undefined")', async () => {
  assert.equal(await runTool('read_file', {}), "ERROR: 'path'");
  assert.equal(await runTool('write_file', {path: 'x'}), "ERROR: 'content'");
  assert.equal(await runTool('execute_command', {}), "ERROR: 'command'");
});

test('execute_command non-integer timeout → ERROR, command NOT run', async () => {
  const out = await runTool('execute_command', {command: 'Write-Output ran', timeout: '30s'});
  assert.match(out, /^ERROR: invalid timeout:/);
});
