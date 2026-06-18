import test from 'node:test';
import assert from 'node:assert/strict';
import {shlexSplit, buildAgentArgv, Pool} from '../src/agent/handover.js';

test('shlexSplit — whitespace splits; single/double quotes group', () => {
  assert.deepEqual(shlexSplit('a b  c'), ['a', 'b', 'c']);
  assert.deepEqual(shlexSplit('a "b c" d'), ['a', 'b c', 'd']);
  assert.deepEqual(shlexSplit("x 'y z'"), ['x', 'y z']);
});

test('buildAgentArgv — {prompt} stays ONE argv element despite spaces', () => {
  const argv = buildAgentArgv(
    '{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}',
    {bin: 'claude', model: 'claude-opus-4-8', effort: 'high', permission: 'acceptEdits', prompt: 'do the thing with spaces'},
  );
  assert.deepEqual(argv, [
    'claude', '--model', 'claude-opus-4-8', '--effort', 'high',
    '--permission-mode', 'acceptEdits', '--print', 'do the thing with spaces',
  ]);
});

test('buildAgentArgv — unknown {x} left as-is; embedded placeholder substituted', () => {
  assert.deepEqual(
    buildAgentArgv('{bin} --flag={effort} {unknown}', {bin: 'c', model: 'm', effort: 'high', permission: 'p', prompt: 'pr'}),
    ['c', '--flag=high', '{unknown}'],
  );
});

test('Pool — caps concurrency at max', async () => {
  const pool = new Pool(2);
  let active = 0;
  let peak = 0;
  const job = (): Promise<boolean> =>
    pool.run(async () => {
      active++;
      peak = Math.max(peak, active);
      await new Promise((r) => setTimeout(r, 20));
      active--;
      return true;
    });
  await Promise.all([job(), job(), job(), job(), job()]);
  assert.ok(peak <= 2, `peak concurrency ${peak} must be ≤ 2`);
});
