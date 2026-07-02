import test from 'node:test';
import assert from 'node:assert/strict';
import {Server, HttpError} from '../src/net/server.js';
import {chatStream} from '../src/net/stream.js';

// §2 MUST-FIX (memory-and-security-plan): lock Bearer + X-Session-Id header parity with the Python
// client so the open/token security verdict ("byte-equivalent on every gated path") stays true under
// future edits. Pure unit tests — no live server.

test('headers() carries Bearer + X-Session-Id when set, omits both when unset', () => {
  // open profile: neither token nor session → no auth headers at all
  assert.deepEqual(new Server('http://h:8100').headers(), {}, 'open profile sends no auth headers');

  // token + live session → both present
  const sealed = new Server('http://h:8100', {token: 's3cret'});
  sealed.sessionId = 'sid-1';
  const h = sealed.headers();
  assert.equal(h['Authorization'], 'Bearer s3cret', 'Bearer token present');
  assert.equal(h['X-Session-Id'], 'sid-1', 'X-Session-Id present');

  // token only (no session yet) → Bearer present, X-Session-Id omitted
  const tok = new Server('http://h:8100', {token: 's3cret'});
  assert.equal(tok.headers()['Authorization'], 'Bearer s3cret');
  assert.equal(tok.headers()['X-Session-Id'], undefined, 'no session → no X-Session-Id');
});

test('chatStream sends headers() ∪ {X-Local-Tools:1, Content-Type} on /chat/stream', async () => {
  const realFetch = globalThis.fetch;
  let captured: Record<string, string> | undefined;
  let capturedUrl = '';
  globalThis.fetch = (async (url: string, opts: {headers?: Record<string, string>}) => {
    capturedUrl = String(url);
    captured = opts.headers;
    return new Response(new ReadableStream({start: (c) => c.close()}), {status: 200});
  }) as unknown as typeof fetch;
  try {
    const srv = new Server('http://h:8100', {token: 's3cret'});
    srv.sessionId = 'sid-1';
    await chatStream(srv, 'hi', {onText: () => {}});
    assert.match(capturedUrl, /\/chat\/stream$/, 'posts to /chat/stream');
    assert.equal(captured?.['X-Local-Tools'], '1', 'X-Local-Tools:1 always set');
    assert.equal(captured?.['Content-Type'], 'application/json');
    assert.equal(captured?.['Authorization'], 'Bearer s3cret', 'auth header carried on the stream');
    assert.equal(captured?.['X-Session-Id'], 'sid-1', 'session header carried on the stream');
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('req() throws HttpError (with the status) on a non-2xx response', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response('unauthorized', {status: 401})) as unknown as typeof fetch;
  try {
    const srv = new Server('http://h:8100', {token: 'wrong'});
    await assert.rejects(
      () => srv.req('GET', '/tasks'),
      (e: unknown) => e instanceof HttpError && e.status === 401,
      'a 401 surfaces as HttpError(401), not a silently-swallowed empty result',
    );
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('#935 chatStream detects a destructive needs_confirm reply + strips --yes → confirm', async () => {
  const realFetch = globalThis.fetch;
  let sentBody: {message?: string; confirm?: boolean} = {};
  globalThis.fetch = (async (_url: string, opts: {body?: string}) => {
    sentBody = JSON.parse(opts.body ?? '{}');
    return new Response(
      JSON.stringify({ok: true, needs_confirm: {command: 'project delete', tier: 'destructive', reason: 'irreversible'}}),
      {status: 200, headers: {'content-type': 'application/json'}},
    );
  }) as unknown as typeof fetch;
  try {
    const srv = new Server('http://h:8100');
    const res = await chatStream(srv, '/project delete demo', {onText: () => {}});
    assert.equal(res?.needs_confirm?.command, 'project delete');   // detected the JSON confirm reply (not a stream)
    assert.equal(sentBody.confirm, false);                          // no --yes → confirm=false
    await chatStream(srv, '/project delete demo --yes', {onText: () => {}});
    assert.equal(sentBody.confirm, true);                           // --yes → confirm=true
    assert.equal(sentBody.message, '/project delete demo');         // --yes stripped from the message
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('#955 chatStream returns a needs_guide reply for an explicit ?/--guide', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(
    JSON.stringify({ok: true, needs_guide: {command: 'config set', subcommands: [],
      fields: [{name: '<dotted.key>', required: true, choices: [], default: '', type: 'value'}],
      usage: 'usage: /config set <dotted.key> <value>', canonical_echo: '/config set'}}),
    {status: 200, headers: {'content-type': 'application/json'}})) as unknown as typeof fetch;
  try {
    const srv = new Server('http://h:8100');
    const res = await chatStream(srv, '/config set ?', {onText: () => {}});
    assert.equal(res?.needs_guide?.command, 'config set');
    assert.equal(res?.needs_guide?.fields[0]?.name, '<dotted.key>');
  } finally {
    globalThis.fetch = realFetch;
  }
});
