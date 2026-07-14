import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {runTool} from '../src/tools/runTool.js';

/** Run a body with cwd set into `dir`, restoring it afterwards (search_files uses cwd-relative paths). */
async function inDir(dir: string, body: () => Promise<void>): Promise<void> {
  const prev = process.cwd();
  process.chdir(dir);
  try {
    await body();
  } finally {
    process.chdir(prev);
  }
}

test('search_files hit path uses path.sep + 1-based line; nested file found', async () => {
  const d = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-search-'));
  await fs.writeFile(path.join(d, 'a.md'), 'nothing here\nFOObar on line 2');
  await fs.mkdir(path.join(d, 'sub'));
  await fs.writeFile(path.join(d, 'sub', 'b.md'), 'first\nsecond\nfoo deep on line 3');
  await inDir(d, async () => {
    const out = await runTool('search_files', {pattern: 'foo', directory: '.', file_pattern: '*.md'});
    // a.md hit on line 2 (top-level, no separator)
    assert.match(out, /(^|\n)a\.md:2: FOObar on line 2(\n|$)/);
    // nested hit must use the OS separator — backslash on Windows, slash on POSIX
    const nested = `sub${path.sep}b.md:3: foo deep on line 3`;
    assert.ok(out.includes(nested), `nested hit "${nested}" in:\n${out}`);
  });
  await fs.rm(d, {recursive: true, force: true});
});

test('search_files no matches → "No matches"', async () => {
  const d = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-search-'));
  await fs.writeFile(path.join(d, 'a.md'), 'alpha\nbeta');
  await inDir(d, async () => {
    assert.equal(await runTool('search_files', {pattern: 'zzz-nope', directory: '.'}), 'No matches');
  });
  await fs.rm(d, {recursive: true, force: true});
});

test('search_files invalid regex falls back to literal substring', async () => {
  const d = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-search-'));
  await fs.writeFile(path.join(d, 'a.md'), 'has a [bracket here');
  await inDir(d, async () => {
    // "[" is an invalid regex → literal lowercase substring match
    const out = await runTool('search_files', {pattern: '[bracket', directory: '.'});
    assert.match(out, /a\.md:1: has a \[bracket here/);
  });
  await fs.rm(d, {recursive: true, force: true});
});

test('search_files stops immediately at the 50-hit cap and reports truncation', async () => {
  const d = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-search-'));
  await fs.writeFile(path.join(d, 'hits.md'), Array.from({length: 60}, (_, i) => `needle ${i}`).join('\n'));
  await inDir(d, async () => {
    const out = await runTool('search_files', {pattern: 'needle', directory: '.'});
    assert.equal(out.split('\n').length, 51, '50 hits plus one truncation marker');
    assert.match(out, /stopped at the 50-hit cap/);
    assert.doesNotMatch(out, /needle 50/);
  });
  await fs.rm(d, {recursive: true, force: true});
});

test('search_files stops at the derived file-scan budget', async () => {
  const d = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-search-'));
  for (let start = 0; start < 1001; start += 50) {
    await Promise.all(Array.from({length: Math.min(50, 1001 - start)}, (_, offset) => {
      const i = start + offset;
      return fs.writeFile(path.join(d, `f${String(i).padStart(4, '0')}.md`), 'no match');
    }));
  }
  await inDir(d, async () => {
    const out = await runTool('search_files', {pattern: 'needle', directory: '.'});
    assert.match(out, /stopped after the 1000-file scan budget/);
  });
  await fs.rm(d, {recursive: true, force: true});
});
