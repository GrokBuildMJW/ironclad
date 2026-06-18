import test from 'node:test';
import assert from 'node:assert/strict';
import {existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {load, save, clear, transcriptStats, exitMessage, statePath, STATE_VERSION, MAX_TRANSCRIPT_LINES} from '../src/state/persist.js';

function tmp(): {dir: string; file: string; cleanup: () => void} {
  const dir = mkdtempSync(join(tmpdir(), 'ironclad-persist-'));
  return {dir, file: join(dir, 'session.json'), cleanup: () => rmSync(dir, {recursive: true, force: true})};
}

test('save → load round-trips the session state', () => {
  const t = tmp();
  try {
    save({serverUrl: 'http://spark:8100', codedir: '/code', sessionId: 'sid-1', transcript: ['> hi', 'hello'], updatedAt: 123}, t.file);
    const got = load(t.file);
    assert.ok(got);
    assert.equal(got.version, STATE_VERSION);
    assert.equal(got.serverUrl, 'http://spark:8100');
    assert.equal(got.codedir, '/code');
    assert.equal(got.sessionId, 'sid-1');
    assert.deepEqual(got.transcript, ['> hi', 'hello']);
    assert.equal(got.updatedAt, 123);
  } finally {
    t.cleanup();
  }
});

test('load returns null when the file is absent', () => {
  const t = tmp();
  try {
    assert.equal(load(t.file), null);
  } finally {
    t.cleanup();
  }
});

test('load returns null on malformed JSON', () => {
  const t = tmp();
  try {
    writeFileSync(t.file, '{not json', 'utf8');
    assert.equal(load(t.file), null);
  } finally {
    t.cleanup();
  }
});

test('load returns null on a different schema version', () => {
  const t = tmp();
  try {
    writeFileSync(t.file, JSON.stringify({version: STATE_VERSION + 1, transcript: ['x']}), 'utf8');
    assert.equal(load(t.file), null);
  } finally {
    t.cleanup();
  }
});

test('save bounds the transcript to the most recent lines', () => {
  const t = tmp();
  try {
    const many = Array.from({length: MAX_TRANSCRIPT_LINES + 50}, (_, i) => `line ${i}`);
    save({serverUrl: 's', codedir: 'c', sessionId: null, transcript: many, updatedAt: 1}, t.file);
    const got = load(t.file);
    assert.ok(got);
    assert.equal(got.transcript.length, MAX_TRANSCRIPT_LINES);
    assert.equal(got.transcript.at(-1), `line ${MAX_TRANSCRIPT_LINES + 49}`); // newest kept
    assert.equal(got.transcript[0], `line 50`); // oldest dropped
  } finally {
    t.cleanup();
  }
});

test('save is atomic — no .tmp left behind', () => {
  const t = tmp();
  try {
    save({serverUrl: 's', codedir: 'c', sessionId: null, transcript: ['x'], updatedAt: 1}, t.file);
    assert.equal(existsSync(`${t.file}.tmp`), false);
    assert.equal(existsSync(t.file), true);
  } finally {
    t.cleanup();
  }
});

test('persisted file is secret-free — no token/bearer/authorization', () => {
  const t = tmp();
  try {
    save({serverUrl: 'http://spark:8100', codedir: '/c', sessionId: 'sid', transcript: ['hi'], updatedAt: 1}, t.file);
    const raw = readFileSync(t.file, 'utf8').toLowerCase();
    assert.equal(raw.includes('token'), false);
    assert.equal(raw.includes('authorization'), false);
    assert.equal(raw.includes('bearer'), false);
  } finally {
    t.cleanup();
  }
});

test('save is fail-soft — a bad path never throws', () => {
  const t = tmp();
  try {
    // target path is an existing directory → writeFileSync would throw; save must swallow it
    assert.doesNotThrow(() => save({serverUrl: 's', codedir: 'c', sessionId: null, transcript: [], updatedAt: 1}, t.dir));
  } finally {
    t.cleanup();
  }
});

test('clear deletes the persisted session (MEM-12), idempotent + fail-soft', () => {
  const t = tmp();
  try {
    save({serverUrl: 's', codedir: 'c', sessionId: null, transcript: ['x'], updatedAt: 1}, t.file);
    assert.ok(load(t.file));
    clear(t.file);
    assert.equal(load(t.file), null);   // gone → no resume
    clear(t.file);                       // missing file → no throw
  } finally {
    t.cleanup();
  }
});

test('transcriptStats — honest turns + real line count (MEM-14)', () => {
  const t = ['> wer bist du ?', 'Ich bin…\n- a\n- b\n- c', '> noch was', 'kurz'];
  assert.deepEqual(transcriptStats(t), {turns: 2, lines: 7}); // 2 '> ' entries; 1+4+1+1 real lines
  assert.deepEqual(transcriptStats([]), {turns: 0, lines: 0});
});

test('exitMessage — only when a non-empty session is saved (MEM-18)', () => {
  assert.equal(exitMessage(null), '');
  assert.equal(exitMessage({version: 1, serverUrl: 's', codedir: 'c', sessionId: null, transcript: [], updatedAt: 0}), '');
  const msg = exitMessage({version: 1, serverUrl: 's', codedir: 'c', sessionId: null, transcript: ['> hi'], updatedAt: 0});
  assert.match(msg, /\/resume/);
});

test('statePath — per project under <codedir>/.ironclad-cli (MEM-19)', () => {
  const prev = process.env['GX10_STATE'];
  delete process.env['GX10_STATE'];
  try {
    assert.equal(statePath(join('/proj', 'a')), join('/proj', 'a', '.ironclad-cli', 'session.json'));
    assert.notEqual(statePath('/proj/a'), statePath('/proj/b')); // different projects → different files
  } finally {
    if (prev !== undefined) process.env['GX10_STATE'] = prev;
  }
});

test('statePath honors $GX10_STATE (wins over per-project)', () => {
  const prev = process.env['GX10_STATE'];
  process.env['GX10_STATE'] = '/custom/state.json';
  try {
    assert.equal(statePath('/proj/a'), '/custom/state.json');
  } finally {
    if (prev === undefined) delete process.env['GX10_STATE'];
    else process.env['GX10_STATE'] = prev;
  }
});

test('save into a .ironclad-cli dir drops a self-* .gitignore (MEM-19)', () => {
  const t = tmp();
  try {
    const dir = join(t.dir, '.ironclad-cli');
    const file = join(dir, 'session.json');
    save({serverUrl: 's', codedir: t.dir, sessionId: null, transcript: ['x'], updatedAt: 1}, file);
    assert.equal(readFileSync(join(dir, '.gitignore'), 'utf8'), '*\n'); // dir ignores itself → no repo clutter
    assert.ok(load(file)); // and the session still round-trips
  } finally {
    t.cleanup();
  }
});

test('save does NOT write a .gitignore outside a .ironclad-cli dir (override safety)', () => {
  const t = tmp();
  try {
    const file = join(t.dir, 'state.json'); // arbitrary target (e.g. a $GX10_STATE path)
    save({serverUrl: 's', codedir: 'c', sessionId: null, transcript: ['x'], updatedAt: 1}, file);
    assert.equal(existsSync(join(t.dir, '.gitignore')), false); // must not silently git-ignore a user folder
  } finally {
    t.cleanup();
  }
});
