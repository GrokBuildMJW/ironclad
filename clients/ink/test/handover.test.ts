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
  // #1300: after a SUCCESSFUL upload the agent scratch is gone (handover drop + feedback + capture)
  const scratch = join(dir, '.ironclad', 'agent');
  await assert.rejects(fs.access(join(scratch, 'handovers', 'T2_OPUS.md')));
  await assert.rejects(fs.access(join(scratch, 'feedback', 'T2_OPUS-output.md')));
  await assert.rejects(fs.access(join(scratch, 'feedback', 'T2_OPUS-feedback.md')));
  await assert.rejects(fs.access(join(scratch, 'logs', 'T2_OPUS.log')));
});

test('processOne — a coder that emits its result ONLY to stdout is CAPTURED (not leaked to the terminal) and used (#1406)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const writer = join(dir, 'stdout-only.cjs');
  await fs.writeFile(writer, "process.stdout.write('STDOUT_ONLY_RESULT'); process.exit(0);", 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')}`};
  const claimed = new Set(['T1406']);
  const stdoutWrites: string[] = [];
  const originalWrite = process.stdout.write;
  process.stdout.write = ((chunk: string | Uint8Array, ...args: unknown[]) => {
    stdoutWrites.push(Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk));
    void args;
    return true;
  }) as typeof process.stdout.write;
  try {
    const ok = await processOne(srv, {id: 'T1406', agent: 'OPUS', handover: 'do stdout'}, dir, cfg, claimed, () => {});
    assert.equal(ok, true);
  } finally {
    process.stdout.write = originalWrite;
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.['content'], 'STDOUT_ONLY_RESULT');
  assert.deepEqual(stdoutWrites, []);
});

test('processOne — coder stdout and stderr are written to a per-task log; client sees only stderr summary (#1406)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const writer = join(dir, 'noisy.cjs');
  await fs.writeFile(
    writer,
    [
      "process.stdout.write('CODER_STDOUT_FULL');",
      "process.stderr.write('first raw stderr line that must not be dumped\\nsecond diagnostic line\\nfinal diagnostic tail');",
      "process.exit(3);",
    ].join(''),
    'utf8',
  );
  const srv = {
    async feedback(): Promise<Json> {
      return {classification: 'task-failed'};
    },
  } as unknown as Server;
  const logs: string[] = [];
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')}`};
  const ok = await processOne(srv, {id: 'T1406LOG', agent: 'OPUS', handover: 'do noisy'}, dir, cfg, new Set(['T1406LOG']), (m) => logs.push(m));
  assert.equal(ok, false);

  const coderLog = join(dir, '.ironclad', 'agent', 'logs', 'T1406LOG_OPUS.log');
  const logText = await fs.readFile(coderLog, 'utf8');
  assert.match(logText, /^# T1406LOG OPUS \(exit 3\)/);
  assert.match(logText, /## stdout\nCODER_STDOUT_FULL/);
  assert.match(logText, /## stderr\nfirst raw stderr line that must not be dumped\nsecond diagnostic line\nfinal diagnostic tail/);

  assert.ok(logs.some((m) => m.includes('OPUS stderr (')));
  assert.ok(logs.some((m) => m.includes(coderLog)));
  assert.ok(logs.some((m) => m.includes('second diagnostic line | final diagnostic tail')));
  assert.equal(logs.some((m) => m.includes('first raw stderr line that must not be dumped')), false);
  assert.equal(logs.some((m) => m.includes('CODER_STDOUT_FULL')), false);
});

test('processOne — a FAILED run keeps its scratch for diagnosis + retry (#1300)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const failer = join(dir, 'fail.cjs');
  await fs.writeFile(failer, "process.exit(1);", 'utf8');
  const srv = {
    async feedback(): Promise<Json> {
      return {classification: 'task-failed'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${failer.replace(/\\/g, '/')}`};
  const ok = await processOne(srv, {id: 'T4', agent: 'OPUS', handover: 'do w'}, dir, cfg, new Set(['T4']), () => {});
  assert.equal(ok, false);
  // the handover drop survives a failed run — the retry re-reads it and the operator can inspect it
  await fs.access(join(dir, '.ironclad', 'agent', 'handovers', 'T4_OPUS.md'));
});

test('runHandover — the coder is launched in the server-shipped project cwd, not the client codedir (#1307)', async () => {
  const codedir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-cd-'));   // the client's stale startup dir (scratch lives here)
  const projDir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-proj-')); // the active project's code root (server-shipped)
  const writer = join(codedir, 'pwd.cjs');
  // the writer records the CHILD's actual cwd into the {feedback} capture path → surfaces as the feedback
  await fs.writeFile(writer, 'require("fs").writeFileSync(process.argv[2], process.cwd());', 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath,
    agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback}`};
  const item = {id: 'P1', agent: 'OPUS', handover: 'build it', cwd: projDir};
  const ok = await processOne(srv, item, codedir, cfg, new Set(['P1']), () => {});
  assert.equal(ok, true);
  const reported = await fs.realpath(String(calls[0]?.['content']).trim());
  assert.equal(reported, await fs.realpath(projDir)); // launched IN the project code root…
  assert.notEqual(reported, await fs.realpath(codedir)); // …NOT the client's codedir
  // the product tree stays clean: no agent scratch was created under the project code root
  await assert.rejects(fs.access(join(projDir, '.ironclad', 'agent', 'handovers', 'P1_OPUS.md')));
});

test('runHandover — falls back to the client codedir when the server ships no cwd (#1307 back-compat)', async () => {
  const codedir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-fb-'));
  const writer = join(codedir, 'pwd.cjs');
  await fs.writeFile(writer, 'require("fs").writeFileSync(process.argv[2], process.cwd());', 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath,
    agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback}`};
  const ok = await processOne(srv, {id: 'P2', agent: 'OPUS', handover: 'x'}, codedir, cfg, new Set(['P2']), () => {});
  assert.equal(ok, true);
  assert.equal(await fs.realpath(String(calls[0]?.['content']).trim()), await fs.realpath(codedir));
});

test('runHandover — falls back to codedir when the shipped cwd does not exist on this host (#1307 remote/sealed)', async () => {
  const codedir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-rs-'));
  const writer = join(codedir, 'pwd.cjs');
  await fs.writeFile(writer, 'require("fs").writeFileSync(process.argv[2], process.cwd());', 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath,
    agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback}`};
  const ghost = join(codedir, 'does-not-exist-on-this-host'); // shipped by the server but absent here
  const item = {id: 'P3', agent: 'OPUS', handover: 'x', cwd: ghost};
  const ok = await processOne(srv, item, codedir, cfg, new Set(['P3']), () => {});
  assert.equal(ok, true);
  assert.equal(await fs.realpath(String(calls[0]?.['content']).trim()), await fs.realpath(codedir));
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
