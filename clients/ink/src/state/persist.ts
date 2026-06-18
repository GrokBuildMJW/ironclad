/**
 * Session-state persistence (§3b reconnect resilience).
 *
 * Saves the conversation transcript + the active session handle locally so a Spark restart /
 * vLLM reload doesn't lose the session — on the next start the client resumes the scrollback.
 *
 * SECRET-FREE: the deployment token is NEVER persisted — only `serverUrl`, `codedir`, the
 * non-secret `session_id`, and the transcript text. Writes are **atomic** (tmp + rename) and
 * **fail-soft**: any fs / JSON error degrades to a no-op (save) or `null` (load), never throws
 * into the UI. Path (MEM-19): `$GX10_STATE`, else PER PROJECT in
 * `<codedir>/.ironclad-cli/session.json` — so each project keeps its own session and it stays out
 * of `%APPDATA%`/Roaming (OneDrive latency on business Windows).
 */
import {existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import {basename, dirname, join} from 'node:path';

export const STATE_VERSION = 1;
/** Bound the persisted transcript so the file can't grow without limit — keep the most recent. */
export const MAX_TRANSCRIPT_LINES = 400;

export interface SessionState {
  version: number;
  serverUrl: string;
  codedir: string;
  sessionId: string | null;
  transcript: string[];
  updatedAt: number;
}

/** The mutable parts a caller supplies; `save` stamps the version + bounds the transcript. */
export type SessionSnapshot = Omit<SessionState, 'version'>;

/** MEM-19: the state file lives PER PROJECT in a `.ironclad-cli/` directory inside the codedir
 *  (like `.git`/`.vscode`) — `<codedir>/.ironclad-cli/session.json`. So switching projects never
 *  overwrites another project's session, and it stays out of `%APPDATA%`/Roaming (which can be
 *  OneDrive-synced on business Windows → load latency). `$GX10_STATE` overrides with one explicit
 *  path (e.g. `%LOCALAPPDATA%` if the project itself sits in OneDrive). */
export function statePath(codedir: string): string {
  return process.env['GX10_STATE'] ?? join(codedir, '.ironclad-cli', 'session.json');
}

/** Make the state directory self-ignoring: drop a `*` .gitignore so a project-local `.ironclad-cli/`
 *  never lands in the user's repo — no edit to their own .gitignore needed. Only our own dir, never
 *  a `$GX10_STATE` override location (we must not silently git-ignore an arbitrary user folder). */
function ensureSelfIgnore(dir: string): void {
  if (basename(dir) !== '.ironclad-cli') return;
  const gi = join(dir, '.gitignore');
  try {
    if (!existsSync(gi)) writeFileSync(gi, '*\n', 'utf8');
  } catch {
    /* fail-soft */
  }
}

/** Load persisted state, or `null` when absent / unreadable / malformed / a different version.
 *  Never throws. */
export function load(path: string): SessionState | null {
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch {
    return null; // not present / unreadable
  }
  try {
    const o = JSON.parse(text) as Partial<SessionState> | null;
    if (!o || typeof o !== 'object' || o.version !== STATE_VERSION) return null;
    return {
      version: STATE_VERSION,
      serverUrl: String(o.serverUrl ?? ''),
      codedir: String(o.codedir ?? ''),
      sessionId: o.sessionId == null ? null : String(o.sessionId),
      transcript: Array.isArray(o.transcript) ? o.transcript.map((x) => String(x)) : [],
      updatedAt: Number(o.updatedAt ?? 0),
    };
  } catch {
    return null; // malformed JSON → ignore
  }
}

/** Atomically persist *snap* (write a tmp file, then rename over the target). Bounds the
 *  transcript to the most recent lines. Fail-soft: any error is swallowed. */
export function save(snap: SessionSnapshot, path: string): void {
  const rec: SessionState = {
    version: STATE_VERSION,
    serverUrl: snap.serverUrl,
    codedir: snap.codedir,
    sessionId: snap.sessionId,
    transcript: snap.transcript.slice(-MAX_TRANSCRIPT_LINES),
    updatedAt: snap.updatedAt,
  };
  try {
    const dir = dirname(path);
    mkdirSync(dir, {recursive: true});
    ensureSelfIgnore(dir); // keep a project-local .ironclad-cli/ out of the user's repo
    const tmp = `${path}.tmp`;
    writeFileSync(tmp, JSON.stringify(rec), 'utf8');
    renameSync(tmp, path); // atomic replace — a crash mid-write never corrupts the live file
  } catch {
    /* fail-soft: persistence is best-effort, never breaks the turn */
  }
}

/** MEM-14: honest counts for the resume banner. A transcript entry is a TURN line (`> …`) or a
 *  whole answer block (multi-line), so `transcript.length` ≠ visible lines. Returns the number of
 *  turns (`> ` entries) and the true visible line count. */
export function transcriptStats(transcript: string[]): {turns: number; lines: number} {
  let turns = 0;
  let lines = 0;
  for (const e of transcript) {
    if (e.startsWith('> ')) turns += 1;
    lines += String(e).split('\n').length;
  }
  return {turns, lines};
}

/** MEM-18: the goodbye line shown on exit when a non-empty session is saved (so the user knows it
 *  can be brought back with /resume) — like other code CLIs. "" when nothing worth resuming. Pure. */
export function exitMessage(saved: SessionState | null): string {
  return saved && saved.transcript.length
    ? '  Sitzung gespeichert — nächstes Mal mit /resume (oder --resume) wiederherstellen.'
    : '';
}

/** MEM-12: delete the persisted session (`/reset`) so it can't resurrect a stale or inconsistent
 *  transcript. Fail-soft — a missing file or fs error is a no-op, never throws. */
export function clear(path: string): void {
  try {
    rmSync(path, {force: true});
  } catch {
    /* fail-soft */
  }
}
