import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {loadConfig, configPath, parseArgs} from '../src/config.js';

// Helpers that snapshot + restore the env keys these tests touch, so cases don't bleed.
const KEYS = ['GX10_CONFIG', 'GX10_SERVER_URL', 'GX10_MAX_AGENTS', 'GX10_SERVER_TOKEN', 'GX10_SRC'];
function withEnv(overrides: Record<string, string | undefined>, fn: () => void): void {
  const saved: Record<string, string | undefined> = {};
  for (const k of KEYS) saved[k] = process.env[k];
  for (const k of KEYS) delete process.env[k];
  for (const [k, v] of Object.entries(overrides)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  try {
    fn();
  } finally {
    for (const k of KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  }
}

function writeConfig(obj: unknown): string {
  const dir = mkdtempSync(join(tmpdir(), 'iron-cfg-'));
  const p = join(dir, 'config.json');
  writeFileSync(p, typeof obj === 'string' ? obj : JSON.stringify(obj), 'utf8');
  return p;
}

test('config file supplies the server URL when no env override', () => {
  const p = writeConfig({serverUrl: 'http://file-host:8100', maxAgents: 7});
  withEnv({GX10_CONFIG: p}, () => {
    const c = loadConfig();
    assert.equal(c.serverUrl, 'http://file-host:8100', 'URL read from the config file');
    assert.equal(c.maxAgents, 7, 'maxAgents read from the config file');
  });
  rmSync(p, {force: true});
});

test('srcDir (MEM-17): null by default, from file, env wins', () => {
  withEnv({GX10_CONFIG: join(tmpdir(), 'absent-cfg.json')}, () => {
    assert.equal(loadConfig().srcDir, null, 'unset → null (/update reports it needs GX10_SRC)');
  });
  const p = writeConfig({srcDir: '/repo/from/file'});
  withEnv({GX10_CONFIG: p}, () => {
    assert.equal(loadConfig().srcDir, '/repo/from/file', 'srcDir read from the config file');
  });
  withEnv({GX10_CONFIG: p, GX10_SRC: '/repo/from/env'}, () => {
    assert.equal(loadConfig().srcDir, '/repo/from/env', 'GX10_SRC wins over the file');
  });
  rmSync(p, {force: true});
});

test('env overrides the config file (file < env)', () => {
  const p = writeConfig({serverUrl: 'http://file-host:8100'});
  withEnv({GX10_CONFIG: p, GX10_SERVER_URL: 'http://env-host:8100'}, () => {
    assert.equal(loadConfig().serverUrl, 'http://env-host:8100', 'env wins over the file');
  });
  rmSync(p, {force: true});
});

test('missing config file falls back to defaults (no throw)', () => {
  withEnv({GX10_CONFIG: join(tmpdir(), 'definitely-absent-iron-config.json')}, () => {
    assert.equal(loadConfig().serverUrl, 'http://localhost:8100', 'default URL');
    assert.equal(loadConfig().maxAgents, 3, 'default maxAgents');
  });
});

test('malformed config file is ignored, not fatal', () => {
  const p = writeConfig('{ this is : not json ');
  withEnv({GX10_CONFIG: p}, () => {
    assert.equal(loadConfig().serverUrl, 'http://localhost:8100', 'malformed → defaults, no crash');
  });
  rmSync(p, {force: true});
});

test('parseArgs — MEM-14 resume is opt-in (default fresh)', () => {
  const prevR = process.env['GX10_RESUME'];
  const prevN = process.env['GX10_NO_RESUME'];
  delete process.env['GX10_RESUME'];
  delete process.env['GX10_NO_RESUME'];
  try {
    const cfg = loadConfig();
    assert.equal(parseArgs([], cfg).resume, false, 'default: fresh (no resume)');
    assert.equal(parseArgs(['--resume'], cfg).resume, true);
    assert.equal(parseArgs(['--fresh'], cfg).resume, false);
    assert.equal(parseArgs(['--no-resume'], cfg).resume, false);
    process.env['GX10_RESUME'] = '1';
    assert.equal(parseArgs([], loadConfig()).resume, true, 'GX10_RESUME → resume');
    process.env['GX10_NO_RESUME'] = '1';
    assert.equal(parseArgs([], loadConfig()).resume, false, 'GX10_NO_RESUME wins over GX10_RESUME');
    assert.equal(parseArgs(['--resume'], loadConfig()).resume, true, '--resume overrides env');
  } finally {
    if (prevR === undefined) delete process.env['GX10_RESUME'];
    else process.env['GX10_RESUME'] = prevR;
    if (prevN === undefined) delete process.env['GX10_NO_RESUME'];
    else process.env['GX10_NO_RESUME'] = prevN;
  }
});

test('configPath honors GX10_CONFIG, else resolves under an ironclad/ dir', () => {
  withEnv({GX10_CONFIG: '/tmp/custom/path.json'}, () => {
    assert.equal(configPath(), '/tmp/custom/path.json');
  });
  withEnv({}, () => {
    assert.match(configPath(), /[\\/]ironclad[\\/]config\.json$/, 'default path ends in ironclad/config.json');
  });
});
