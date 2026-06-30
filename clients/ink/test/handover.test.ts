import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {shlexSplit, buildAgentArgv, resolveLaunch, processOne, Pool, type HandoverCfg} from '../src/agent/handover.js';
import type {Server, Json} from '../src/net/server.js';

const baseCfg: HandoverCfg = {
  claudeBinOverride: null,
  agentCmdOverride: null,
  claudeEffort: 'high',
  claudePermissionMode: 'acceptEdits',
};

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

test('buildAgentArgv — {feedback} substituted, {mcp} expands to multiple args (INK-HANDOVER-1)', () => {
  const argv = buildAgentArgv('{bin} {mcp} -o {feedback} --print {prompt}', {
    bin: 'codex', model: 'm', effort: 'high', permission: 'p', prompt: 'do it',
    feedback: '.ironclad/agent/feedback/x-output.md', mcp: '--mcp-config /tmp/m.json --foo bar',
  });
  assert.deepEqual(argv, [
    'codex', '--mcp-config', '/tmp/m.json', '--foo', 'bar',
    '-o', '.ironclad/agent/feedback/x-output.md', '--print', 'do it',
  ]);
});

test('buildAgentArgv — empty {mcp} contributes no args (INK-HANDOVER-1)', () => {
  assert.deepEqual(
    buildAgentArgv('{bin} {mcp} --print {prompt}', {bin: 'claude', model: 'm', effort: 'e', permission: 'p', prompt: 'go'}),
    ['claude', '--print', 'go'],
  );
});

test('resolveLaunch — the server per-agent spec drives bin/template/model/effort/permission/mcp (INK-HANDOVER-1)', () => {
  const spec = resolveLaunch(
    {agent: 'SONNET', bin: '/srv/bin/codex', cmd_template: '{bin} run {prompt}', model: 'srv-model',
     effort: 'low', permission: 'plan', mcp: '--mcp x', mcp_env: {TOK: 'v'}},
    baseCfg,
  );
  assert.equal(spec.bin, '/srv/bin/codex');
  assert.equal(spec.template, '{bin} run {prompt}');
  assert.equal(spec.model, 'srv-model');
  assert.equal(spec.effort, 'low');
  assert.equal(spec.permission, 'plan');
  assert.equal(spec.mcp, '--mcp x');
  assert.deepEqual(spec.mcpEnv, {TOK: 'v'});
});

test('resolveLaunch — an explicit client override beats the item; defaults fill the rest (INK-HANDOVER-1)', () => {
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: '/usr/local/bin/claude', agentCmdOverride: '{bin} --print {prompt}'};
  const spec = resolveLaunch({agent: 'OPUS', bin: '/srv/ignored', cmd_template: '{bin} ignored'}, cfg);
  assert.equal(spec.bin, '/usr/local/bin/claude'); // explicit override wins over the item spec
  assert.equal(spec.template, '{bin} --print {prompt}');
  assert.equal(spec.model, 'claude-opus-4-8'); // item omits model → OPUS default
  assert.equal(spec.effort, 'high'); // cfg default
  assert.equal(spec.permission, 'acceptEdits'); // cfg default
  assert.deepEqual(spec.mcpEnv, {});
});

test('processOne — ALWAYS reports the run signal even when the binary is missing (INK-HANDOVER-2)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'agent-unavailable'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, agentCmdOverride: '{bin} {prompt}'};
  const item = {id: 'T1', agent: 'OPUS', handover: 'do x', bin: 'definitely-no-such-binary-xyz123'};
  const claimed = new Set(['T1']);
  const ok = await processOne(srv, item, dir, cfg, claimed, () => {});
  assert.equal(ok, false);
  assert.equal(calls.length, 1); // the run signal is POSTed despite no feedback (the #455 breaker is reachable)
  assert.equal(calls[0]?.['content'], '');
  assert.equal(calls[0]?.['exit_code'], null);
  assert.equal(calls[0]?.['stderr'], 'binary-not-found');
  assert.equal(claimed.has('T1'), false); // un-claimed for retry/failover
});

test('processOne — a nonzero exit with stderr and no feedback still reports the run signal (#455 failover)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const failer = join(dir, 'fail.cjs');
  await fs.writeFile(failer, "process.stderr.write('boom: quota exceeded'); process.exit(7);", 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'agent-unavailable'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${failer.replace(/\\/g, '/')}`};
  const claimed = new Set(['T3']);
  const ok = await processOne(srv, {id: 'T3', agent: 'OPUS', handover: 'do z'}, dir, cfg, claimed, () => {});
  assert.equal(ok, false);
  assert.equal(calls.length, 1); // the #455 breaker path: budget-exhausted run is reported, not silently retried
  assert.equal(calls[0]?.['content'], '');
  assert.equal(calls[0]?.['exit_code'], 7);
  assert.match(String(calls[0]?.['stderr']), /quota exceeded/);
  assert.equal(claimed.has('T3'), false);
});

test('processOne — uploads the captured final message via the {feedback} fallback (ok path)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const writer = join(dir, 'writer.cjs');
  await fs.writeFile(writer, "require('fs').writeFileSync(process.argv[2], 'CAPTURED');", 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as unknown as Server;
  // bin = this node; template runs the writer with the {feedback} capture path as its arg
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback}`};
  const claimed = new Set(['T2']);
  const ok = await processOne(srv, {id: 'T2', agent: 'OPUS', handover: 'do y'}, dir, cfg, claimed, () => {});
  assert.equal(ok, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.['content'], 'CAPTURED'); // the {feedback} capture is read when no feedback file is written
  assert.equal(calls[0]?.['exit_code'], 0);
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
