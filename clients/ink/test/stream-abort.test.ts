import test from 'node:test';
import assert from 'node:assert/strict';
import {chatStream} from '../src/net/stream.js';
import {Server} from '../src/net/server.js';

// The external cancel signal (Esc / Ctrl+C in the UI) must abort the in-flight fetch immediately,
// so a hung/thinking-runaway turn returns to idle at once instead of waiting on the server.
test('chatStream aborts the request when the external signal fires', async () => {
  const realFetch = globalThis.fetch;
  // a fetch that honours the AbortSignal: it never resolves on its own, only rejects on abort
  globalThis.fetch = ((_url: string, opts: {signal?: AbortSignal}) =>
    new Promise((_resolve, reject) => {
      const sig = opts.signal;
      if (sig?.aborted) return reject(new DOMException('aborted', 'AbortError'));
      sig?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    })) as unknown as typeof fetch;
  try {
    const srv = new Server('http://127.0.0.1:1', {timeoutMs: 60_000});
    const ac = new AbortController();
    const p = chatStream(srv, 'wer bist du', {onText: () => {}}, ac.signal);
    ac.abort(); // user hits Esc / Ctrl+C
    await assert.rejects(p, (e: Error) => /abort/i.test(e.name) || /abort/i.test(e.message));
  } finally {
    globalThis.fetch = realFetch;
  }
});
