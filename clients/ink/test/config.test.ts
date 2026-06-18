import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {loadConfig, configPath} from '../src/config.js';

// Helpers that snapshot + restore the env keys these tests touch, so cases don't bleed.
const KEYS = ['GX10_CONFIG', 'GX10_SERVER_URL', 'GX10_MAX_AGENTS', 'GX10_SERVER_TOKEN'];
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

test('configPath honors GX10_CONFIG, else resolves under an ironclad/ dir', () => {
  withEnv({GX10_CONFIG: '/tmp/custom/path.json'}, () => {
    assert.equal(configPath(), '/tmp/custom/path.json');
  });
  withEnv({}, () => {
    assert.match(configPath(), /[\\/]ironclad[\\/]config\.json$/, 'default path ends in ironclad/config.json');
  });
});
