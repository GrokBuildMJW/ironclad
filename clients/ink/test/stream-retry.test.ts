import test from 'node:test';
import assert from 'node:assert/strict';
import {HttpError, Server} from '../src/net/server.js';
import {chatStream} from '../src/net/stream.js';

const BUSY = 'busy - another turn is still running; try again shortly';

function busyResponse(): Response {
  return new Response(JSON.stringify({ok: false, error: BUSY}), {
    status: 503,
    headers: {'content-type': 'application/json'},
  });
}

test('#1650 retries a busy 503 and completes the successful streamed turn', async () => {
  const realFetch = globalThis.fetch;
  let calls = 0;
  const retries: string[] = [];
  globalThis.fetch = (async () => {
    calls++;
    return calls === 1 ? busyResponse() : new Response('turn completed', {status: 200});
  }) as unknown as typeof fetch;
  try {
    const text: string[] = [];
    await chatStream(new Server('http://h:8100'), 'hi', {
      onText: (chunk) => text.push(chunk),
      onRetry: (reason) => retries.push(reason),
    });
    assert.equal(calls, 2);
    assert.deepEqual(retries, [BUSY]);
    assert.equal(text.join(''), 'turn completed');
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('#1650 exhausted 503 retries throw the engine reason', async () => {
  const realFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls++;
    return busyResponse();
  }) as unknown as typeof fetch;
  try {
    await assert.rejects(
      () => chatStream(new Server('http://h:8100'), 'hi', {onText: () => {}}),
      (e: unknown) => e instanceof HttpError && e.status === 503 && e.message === BUSY,
    );
    assert.equal(calls, 3);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('#1650 a 4xx is not retried and surfaces its JSON error', async () => {
  const realFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls++;
    return new Response(JSON.stringify({ok: false, error: "missing 'message'"}), {status: 400});
  }) as unknown as typeof fetch;
  try {
    await assert.rejects(
      () => chatStream(new Server('http://h:8100'), '', {onText: () => {}}),
      (e: unknown) => e instanceof HttpError && e.status === 400 && e.message === "missing 'message'",
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('#1650 aborting during the retry wait rejects promptly and stops retrying', async () => {
  const realFetch = globalThis.fetch;
  let calls = 0;
  const ac = new AbortController();
  globalThis.fetch = (async () => {
    calls++;
    return busyResponse();
  }) as unknown as typeof fetch;
  try {
    const started = Date.now();
    const p = chatStream(new Server('http://h:8100', {timeoutMs: 60_000}), 'hi', {
      onText: () => {},
      onRetry: () => ac.abort(),
    }, ac.signal);
    await assert.rejects(p, (e: unknown) => e instanceof DOMException && e.name === 'AbortError');
    assert.ok(Date.now() - started < 250, 'abort does not wait through the bounded retry sequence');
    await new Promise((resolve) => setTimeout(resolve, 150));
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('#1650 an unusable error body falls back to the HTTP status', async () => {
  const realFetch = globalThis.fetch;
  try {
    for (const body of ['not json', '', '{}']) {
      let calls = 0;
      globalThis.fetch = (async () => {
        calls++;
        return new Response(body, {status: 400});
      }) as unknown as typeof fetch;
      await assert.rejects(
        () => chatStream(new Server('http://h:8100'), 'hi', {onText: () => {}}),
        (e: unknown) => e instanceof HttpError && e.message === 'POST /chat/stream → HTTP 400',
      );
      assert.equal(calls, 1);
    }
  } finally {
    globalThis.fetch = realFetch;
  }
});
