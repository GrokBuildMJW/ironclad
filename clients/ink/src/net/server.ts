/**
 * HTTP client to the orchestrator — a port of engine/client.py:Server (JSON methods).
 * The streaming turn (/chat/stream + the \x00TR tool-bridge) lives in net/stream.ts.
 *
 * Parity notes: urllib.urlopen raises HTTPError on non-2xx; fetch does not, so _req throws
 * an HttpError on !ok so callers (e.g. session_open's 401 path) can branch the same way.
 * _headers() carries Authorization (Bearer token) + X-Session-Id on EVERY request, incl.
 * the gated ones (/tool-result, /cancel, /tasks, /pending, /feedback).
 */
export class HttpError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = 'HttpError';
  }
}

export type Json = Record<string, unknown>;

/** #454: prefer the server's JSON error detail, with the HTTP status as a fail-soft fallback. */
export function httpError(method: string, path: string, status: number, raw: string): HttpError {
  let detail = `${method} ${path} → HTTP ${status}`;
  try {
    const j = JSON.parse(raw) as Json;
    if (typeof j['error'] === 'string') detail = j['error'] as string;
  } catch {
    /* non-JSON body → keep the generic detail */
  }
  return new HttpError(status, detail);
}

export class Server {
  readonly base: string;
  readonly timeoutMs: number;
  token: string | null;
  sessionId: string | null = null;

  constructor(baseUrl: string, opts: {timeoutMs?: number; token?: string | null} = {}) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.timeoutMs = opts.timeoutMs ?? 600_000;
    this.token = opts.token ?? null;
  }

  headers(): Record<string, string> {
    const h: Record<string, string> = {};
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    if (this.sessionId) h['X-Session-Id'] = this.sessionId;
    return h;
  }

  async req(method: string, path: string, body?: Json, signal?: AbortSignal): Promise<Json> {
    const headers: Record<string, string> = {...this.headers()};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    // #1539: an optional caller-supplied signal (a session-lifecycle abort or a short shutdown deadline) is
    // combined with the per-request timeout — whichever fires first wins. This lets session.stop() abort a
    // stuck in-flight heartbeat immediately instead of waiting out the full 600s request timeout.
    const timeout = AbortSignal.timeout(this.timeoutMs);
    const res = await fetch(this.base + path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: signal ? AbortSignal.any([timeout, signal]) : timeout,
    });
    const raw = await res.text();
    if (!res.ok) {
      // #454: surface the server's JSON {error: …} detail (e.g. 'unknown agent …' from POST /coders)
      // so all four clients show the same friendly message; fall back to the generic status line.
      throw httpError(method, path, res.status, raw);
    }
    return raw ? (JSON.parse(raw) as Json) : {};
  }

  health(signal?: AbortSignal): Promise<Json> {
    return this.req('GET', '/health', undefined, signal);
  }

  /** DOCTOR (#503): gated read-only preflight report — local `/doctor` command (mirrors `/health`). */
  doctor(): Promise<Json> {
    return this.req('GET', '/doctor');
  }

  async tasks(signal?: AbortSignal): Promise<Json[]> {
    const r = await this.req('GET', '/tasks', undefined, signal);
    return (r['tasks'] as Json[]) ?? [];
  }

  async pending(): Promise<Json[]> {
    const r = await this.req('GET', '/pending');
    return (r['pending'] as Json[]) ?? [];
  }

  claim(taskId: string, agent: string): Promise<Json> {
    return this.req('POST', '/claim', {task_id: taskId, agent});
  }

  unclaim(taskId: string): Promise<Json> {
    return this.req('POST', '/unclaim', {task_id: taskId});
  }

  /** #452: which coding agents are bound (registry + boot probe) + the fan-out provider lane. */
  async coders(): Promise<Json> {
    return this.req('GET', '/coders');
  }

  /** #454: pin the runtime coding agent (`auto`/null clears it). Throws HttpError(400) on unknown. */
  async setCoderPin(agent: string): Promise<Json> {
    return this.req('POST', '/coders', {agent});
  }

  /** Loaded prompt/skill registry snapshot (#149) — backs slash autocomplete. Same
   *  `_catalogue_snapshot` that powers the `/prompts`/`/skills` commands server-side. */
  async catalogue(): Promise<{prompts: Json[]; skills: Json[]; commands: Json[]}> {
    const r = await this.req('GET', '/catalogue');
    return {
      prompts: (r['prompts'] as Json[]) ?? [],
      skills: (r['skills'] as Json[]) ?? [],
      commands: (r['commands'] as Json[]) ?? [],   // #931: server-command spec for generated completions
    };
  }

  chat(message: string): Promise<Json> {
    return this.req('POST', '/chat', {message});
  }

  cancel(): Promise<Json> {
    return this.req('POST', '/cancel', {});
  }

  feedback(body: Json): Promise<Json> {
    return this.req('POST', '/feedback', body);
  }

  // ── session lifecycle (Phase d; no-op transport-wise on the open profile) ──
  async sessionOpen(signal?: AbortSignal): Promise<Json> {
    const res = await this.req('POST', '/session/open', {}, signal);
    this.sessionId = (res['session_id'] as string) ?? null;
    return res;
  }

  async sessionHeartbeat(signal?: AbortSignal): Promise<boolean> {
    if (!this.sessionId) return false;
    try {
      const r = await this.req('POST', '/session/heartbeat', {session_id: this.sessionId}, signal);
      return Boolean(r['ok']);
    } catch {
      return false;
    }
  }

  async sessionClose(signal?: AbortSignal): Promise<void> {
    if (!this.sessionId) return;
    try {
      await this.req('POST', '/session/close', {session_id: this.sessionId}, signal);
    } catch {
      /* network gone / aborted — nothing more to clean up remotely (server-side session expires at its TTL) */
    }
    this.sessionId = null;
  }
}
