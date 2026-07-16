/**
 * Local tool-bridge ≙ gx10.run_tool's 9 `LOCAL_TOOL_NAMES` (gx10.py:1631-1767),
 * reimplemented cross-platform in Node. `runTool(name, args)` ALWAYS resolves to a string
 * (success or `ERROR: …`); return strings + caps are byte-for-byte identical to the Python
 * source because the model is prompt-tuned to them. All FS paths are relative to
 * `process.cwd()` (= `--codedir`, set once at startup).
 *
 * Parity note (must-fix, gx10.py:1651-1657): `write_file` is a PLAIN write+rename with NO
 * retry loop — the `_atomic_write` PermissionError fallback is wired only into the
 * handover/TaskStore paths, not this tool. We match the source: no retry by default.
 *
 * Accepted parity limits (audited 2026-06-17, deliberately NOT fixed — not byte-fixable or
 * not worth it): a generic OS exception leaks Node's `err.message`, not Python's `str(e)`
 * (different runtimes — never identical; the model keys on the success / known-error strings,
 * not OS leaks); symlink type-resolution (Dirent vs is_dir following links); signal-terminated
 * exit codes; `.trim()` Unicode-whitespace set; float-timeout message precision. Everything
 * else (rglob traversal order, move/copy-into-existing-dir, code-point lengths, splitlines,
 * int() coercion incl. bool, required-arg guards) is matched to the source.
 */
import {promises as fs, type Dirent} from 'node:fs';
import path from 'node:path';
import {spawn, type ChildProcess} from 'node:child_process';
import {detectShell, gitBash} from './shell.js';
import {directoryEntryNames, fmtCount, listingTargetForCommand} from './listingCount.js';
import {rglob} from './glob.js';
import {killProcessTree, waitForChildExit} from './procTree.js';
import {emitDiagnosticOnce} from './diagnostics.js';
import {BoundedTail, MAX_CAPTURE_BYTES} from './boundedTail.js';

const MAX_FILE_CHARS = 24000; // gx10.py:102
const LIST_DIR_HARD_CAP = 200; // gx10.py:103
// #1488: cap bytes before decoding/allocation. MAX_FILE_BYTES is the ALLOCATION ceiling (OOM protection),
// NOT the model-output cap (that stays MAX_FILE_CHARS) — high enough to load a normal repo file so #1047
// ranged/pattern reads of large files keep working, low enough to refuse a multi-GB file. Search is
// decoupled: it scans many files one-at-a-time, so its per-file cap is kept small. Kept in sync with
// gx10.py (_MAX_FILE_BYTES / _SEARCH_MAX_FILE_BYTES) so a file refused server-side is refused client-side.
const MAX_FILE_BYTES = 16 * 1024 * 1024;
const SEARCH_MAX_FILES = LIST_DIR_HARD_CAP * 5;
const SEARCH_MAX_FILE_BYTES = 1024 * 1024;
const SEARCH_HIT_CAP = 50;

type Args = Record<string, unknown>;

// ── helpers ──────────────────────────────────────────────────────────────────
function errCode(e: unknown): string | undefined {
  return e && typeof e === 'object' && 'code' in e ? String((e as {code?: unknown}).code) : undefined;
}

/** Required string arg; throws if missing/null so the outer catch returns an ERROR string
 *  instead of operating on "undefined"/"null" (≙ Python KeyError/TypeError on a missing key). */
function reqStr(args: Args, key: string): string {
  const v = args[key];
  if (v === undefined || v === null) throw new Error(`'${key}'`); // ≙ KeyError repr → ERROR: 'key'
  return String(v);
}

/** ≙ Python int(): bool → 1/0; number → truncate; clean integer string → parse; else null. */
function pyInt(v: unknown): number | null {
  if (typeof v === 'boolean') return v ? 1 : 0; // ≙ int(True)=1, int(False)=0
  if (typeof v === 'number') return Number.isFinite(v) ? Math.trunc(v) : null;
  if (typeof v === 'string' && /^[+-]?\d+$/.test(v.trim())) return parseInt(v.trim(), 10);
  return null;
}

/** ≙ str(Path(p)): collapse `.` and repeated separators, keep `..`, OS separator. */
function pyPathStr(p: string): string {
  const segs = p.split(/[\\/]+/).filter((s) => s !== '' && s !== '.');
  return segs.length ? segs.join(path.sep) : '.';
}

// ≙ Python str.splitlines(): CRLF/CR/LF plus VT FF FS GS RS NEL LS PS. The exotic separators
// are injected via String.fromCharCode so NO literal line terminator (esp. U+2028/U+2029,
// which would break a regex literal) ever appears in the source.
const LINE_BOUNDARY = new RegExp('\\r\\n|[\\n\\r\\v\\f' + String.fromCharCode(0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029) + ']');
function pySplitlines(s: string): string[] {
  if (s === '') return [];
  const lines = s.split(LINE_BOUNDARY);
  if (lines[lines.length - 1] === '') lines.pop(); // no trailing empty from a terminal break
  return lines;
}

async function mkdirp(dir: string): Promise<void> {
  if (dir) await fs.mkdir(dir, {recursive: true});
}

/** Stat first (so a sparse/known-large file is rejected without reading), then protect the stat/open race
 * with a byte-limited fd read. The single allocation is fixed and never scales with the source file. */
async function readBoundedFile(
  p: string,
  maxBytes: number,
  rejectKnownOversize = true,
): Promise<{buf: Buffer; size: number; over: boolean}> {
  const fh = await fs.open(p, 'r');
  try {
    let size = Number((await fh.stat()).size);
    if (size > maxBytes && rejectKnownOversize) return {buf: Buffer.alloc(0), size, over: true};
    const buf = Buffer.allocUnsafe(maxBytes + 1);
    let offset = 0;
    while (offset < buf.length) {
      const {bytesRead} = await fh.read(buf, offset, buf.length - offset, offset);
      if (bytesRead === 0) break;
      offset += bytesRead;
    }
    if (offset > maxBytes) size = Math.max(size, Number((await fh.stat()).size), offset);
    return {buf: buf.subarray(0, Math.min(offset, maxBytes)), size, over: size > maxBytes || offset > maxBytes};
  } finally {
    await fh.close();
  }
}

/** ≙ subprocess.run with DEVNULL stdin, UTF-8-lossy capture, timeout. */
// #459: harden the PowerShell invocation — prepend $ProgressPreference='SilentlyContinue' so WriteProgress
// can never draw a progress bar into the renderer-owned conhost (the verified scaling break). Mirrors the
// Python server branch. Pure → unit-testable.
export function winPowershellArgs(command: string): string[] {
  return ['-NoProfile', '-NonInteractive', '-Command', `$ProgressPreference='SilentlyContinue'; ${command}`];
}

// #459 (review B S2): the SAME fail-closed shell guardrail as the Python gx10._shell_guard — ported so the
// LOCAL Ink paths (a bridged execute_command AND the user's `/sh` escape hatch, which never reach the
// server-side guard) refuse a remote/web fetch or an unbounded/progress-emitting process before it can
// corrupt the renderer (the verified scaling break). Returns the block reason, or null when allowed. Pure;
// must stay byte-for-byte equivalent to the Python deny-list (parity).
const SHELL_DENY: ReadonlyArray<readonly [RegExp, string]> = [
  // unambiguous web/remote APIs — match anywhere
  [/\b(invoke-webrequest|invoke-restmethod|start-bitstransfer|net\.webclient|downloadstring|downloadfile|system\.net\.(http|webclient))\b/i,
    'a remote/web fetch'],
  // bare fetch commands — only at a COMMAND position (start / after a separator), so a filename/search
  // string merely CONTAINING the token isn't blocked
  [/(?:^|[\n;|&({`])\s*(curl|wget|iwr|irm)\b/i, 'a remote/web fetch'],
  // long-running / progress-emitting processes
  [/(\bstart-sleep\b|while\s*\(\s*\$?true\s*\)|for\s*\(\s*;\s*;|\bping\s+-t\b|\bping\s+-n\s*\d{3,}|\bstart-job\b|\bstart-process\b|\bregister-scheduledtask\b|\bget-content\b[^\n|]*\s-wait\b|\btail\s+-f\b|\bwatch-\w|\btcpdump\b|\bhtop\b)/i,
    'a long-running / progress-emitting process'],
];

export function shellGuard(command: string): string | null {
  const cmd = command ?? '';
  for (const [pat, why] of SHELL_DENY) if (pat.test(cmd)) return why;
  return null;
}

// code null → spawn error / timeout / signal-kill (text carries the ERROR string or raw output);
// the caller needs the exit code to gate the #1193 listing-count prepend on real success.
type ExecResult = {text: string; code: number | null};
type SandboxPolicy = 'auto' | 'bwrap' | 'firejail';
const SANDBOXED_EXEC_WIRE_TOOL = 'execute_command_sandboxed_v1';
export const POST_KILL_DRAIN_MS = 2000;

export function isBestEffortTeardown(backend: string): boolean {
  return backend === 'firejail';
}

async function terminateProcessTree(child: ChildProcess): Promise<void> {
  await killProcessTree(child);
  await waitForChildExit(child, POST_KILL_DRAIN_MS);
}

function collectChild(
  child: ChildProcess, timeoutS: number, spawnError: string | null, signal?: AbortSignal,
): Promise<ExecResult> {
  return new Promise((resolve) => {
    // #1540 (defect B): a rolling bounded tail per stream — a model/operator command that prints gigabytes
    // can no longer OOM the client (the pre-existing unbounded Buffer[] + Buffer.concat allocated the whole
    // stream twice). Shares the coder-handover capture cap (MAX_CAPTURE_BYTES).
    const out = new BoundedTail(MAX_CAPTURE_BYTES);
    const err = new BoundedTail(MAX_CAPTURE_BYTES);
    let done = false;
    let terminating = false;
    let timer: NodeJS.Timeout | undefined;
    const finish = (r: ExecResult): void => {
      if (done) return;
      done = true;
      if (timer) clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      resolve(r);
    };
    const stop = (r: ExecResult): void => {
      if (done || terminating) return;
      terminating = true;
      void terminateProcessTree(child).finally(() => finish(r));
    };
    const abort = (): void => stop({text: 'ERROR: cancelled', code: null});
    timer = setTimeout(() => {
      // #1540 (defect A) / #1491 / #1538: if the child already exited (by code OR signal) but its `close`
      // event is still draining the stdout pipe, do NOT declare a timeout — step aside and let `close`
      // deliver the real exit code + buffered output (mirrors spawnAgent in handover.ts). Otherwise a
      // command that finished exactly at the deadline was reported as a false ERROR: Timeout and its
      // successful output discarded.
      if (done || terminating || child.exitCode !== null || child.signalCode !== null) return;
      stop({text: `ERROR: Timeout after ${timeoutS}s`, code: null});
    }, timeoutS * 1000);
    child.stdout?.on('data', (d: Buffer) => out.append(d));
    child.stderr?.on('data', (d: Buffer) => err.append(d));
    child.on('error', (e) => {
      if (!terminating) finish({text: spawnError ?? `ERROR: ${e.message}`, code: null});
    });
    child.on('close', (code) => {
      if (terminating) return;
      const combined = (out.text() + err.text()).trim();
      finish({text: combined, code});
    });
    signal?.addEventListener('abort', abort, {once: true});
    if (signal?.aborted) abort();
  });
}

function execCommand(command: string, timeoutS: number, cwd: string): Promise<ExecResult> {
  let child: ChildProcess;
  if (process.platform === 'win32') {
    // #1177: route per command so BOTH shells work — a bash command runs in Git Bash (when installed),
    // a PowerShell cmdlet in PowerShell; neither is forced. Falls back to PowerShell without Git Bash.
    const bash = gitBash();
    child = bash && detectShell(command) === 'bash'
      ? spawn(bash, ['-lc', command], {cwd, detached: false, stdio: ['ignore', 'pipe', 'pipe']})
      : spawn('powershell', winPowershellArgs(command), {cwd, detached: false, stdio: ['ignore', 'pipe', 'pipe']});
  } else {
    child = spawn(command, {shell: true, cwd, detached: true, stdio: ['ignore', 'pipe', 'pipe']});
  }
  return collectChild(child, timeoutS, null);
}

async function findSandboxBackend(policy: SandboxPolicy): Promise<'bwrap' | 'firejail' | null> {
  if (process.platform === 'win32') return null;
  const candidates: Array<'bwrap' | 'firejail'> = policy === 'auto' ? ['bwrap', 'firejail'] : [policy];
  const dirs = (process.env.PATH ?? '').split(path.delimiter).filter(Boolean);
  for (const candidate of candidates) {
    for (const dir of dirs) {
      try {
        await fs.access(path.join(dir, candidate));
        return candidate;
      } catch {
        /* keep searching PATH */
      }
    }
  }
  return null;
}

function execSandboxed(
  backend: 'bwrap' | 'firejail', command: string, timeoutS: number, cwd: string, signal?: AbortSignal,
): Promise<ExecResult> {
  // firejail has no clean die-with-parent equivalent, so its tree teardown is best-effort-only.
  const argv = backend === 'bwrap'
    ? ['--die-with-parent', '--unshare-pid', '--dev-bind', '/', '/', '--proc', '/proc', '--unshare-net', '--', 'sh', '-c', command]
    : ['--quiet', '--net=none', '--', 'sh', '-c', command];
  const child = spawn(backend, argv, {
    cwd, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'],
  });
  return collectChild(
    child, timeoutS,
    'ERROR: execute_command refused: mandatory sandbox backend failed to start; Ironclad fails closed.',
    signal,
  );
}

/** Explicit operator channel. It is intentionally separate from model `execute_command` and never used by
 * the tool bridge. The renderer-safety deny-list remains, but no model sandbox policy is applied. */
export async function runOperatorShell(command: string, baseCwd = process.cwd()): Promise<string> {
  const blocked = shellGuard(command);
  if (blocked !== null) return `BLOCKED: operator /sh refuses ${blocked} — it can corrupt the display or hang the session (#459).`;
  const r = await execCommand(command, 30, baseCwd);
  return r.text || `(exit ${r.code ?? 0}, no output)`;
}

/** ≙ Python str.splitlines() (no keepends) for the common separators: split on \r\n / \r / \n and drop the
 *  single phantom trailing '' a trailing separator produces, so the line count matches gx10.py. (Exotic
 *  \v/\f/\x1c separators Python also splits on are an accepted parity limit.) */
function splitLinesLikePython(text: string): string[] {
  if (text === '') return [];
  const lines = text.split(/\r\n|\r|\n/);
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/** #1047: a TARGETED slice of a file's text — a regex `pattern` (a window of lines around the first
 *  match) OR a 1-based inclusive line range start/end, capped by max_chars (else MAX_FILE_CHARS).
 *  Returns null on a bad/empty range or an unmatched/invalid pattern so the caller falls back to the
 *  head+tail cap. Never throws. Mirrors gx10.py `_read_file_ranged` — the return strings are byte-identical
 *  (the model is prompt-tuned to them). The line model is Python str.splitlines() (see splitLinesLikePython)
 *  so counts, indices and the regex `$` anchor match; the returned slice normalises line endings to `\n`. */
function rangedRead(text: string, args: Args): string | null {
  const start = args['start'], end = args['end'], maxChars = args['max_chars'], pattern = args['pattern'];
  if (start === undefined && end === undefined && maxChars === undefined && pattern === undefined) return null;
  try {
    const lines = splitLinesLikePython(text);
    const n = lines.length;
    if (n === 0) return null;
    let lo: number;
    let hi: number;
    if (pattern !== undefined && pattern !== null && String(pattern) !== '') {
      const rx = new RegExp(String(pattern));
      let hit = -1;
      for (let i = 0; i < n; i++) {
        if (rx.test(lines[i] ?? '')) {
          hit = i;
          break;
        }
      }
      if (hit < 0) return null; // no match → fall back to the head+tail cap
      const ctx = 20; // a window of lines around the first match
      lo = Math.max(0, hit - ctx);
      hi = Math.min(n, hit + ctx + 1);
    } else if (start !== undefined || end !== undefined) {
      const s = start !== undefined ? Math.trunc(Number(start)) : 1;
      const e = end !== undefined ? Math.trunc(Number(end)) : n;
      if (!Number.isFinite(s) || !Number.isFinite(e) || s < 1 || s > n || e < s) return null; // bad range
      lo = s - 1;
      hi = Math.min(n, e);
    } else {
      return null;
    }
    let body = lines.slice(lo, hi).join('\n');
    const cp = Array.from(body); // code points ≙ Python len()/str slicing
    const cap = maxChars !== undefined && Number(maxChars) > 0 ? Math.trunc(Number(maxChars)) : MAX_FILE_CHARS;
    if (cap > 0 && cp.length > cap) {
      const headN = Math.floor((cap * 2) / 3);
      const tailN = cap - headN;
      const omitted = cp.length - headN - tailN;
      body =
        cp.slice(0, headN).join('') +
        `\n\n... [Ironclad: ${omitted} chars omitted from the slice — capped at ${cap}] ...\n\n` +
        cp.slice(cp.length - tailN).join('');
    }
    return `[Ironclad: lines ${lo + 1}-${hi} of ${n}]\n${body}`;
  } catch {
    return null;
  }
}

// ── dispatch ─────────────────────────────────────────────────────────────────
export async function runTool(
  name: string, args: Args, baseCwd?: string, sandboxPolicy = 'auto', signal?: AbortSignal,
): Promise<string> {
  // #1317: run the bridged tool in the server-shipped active-project cwd (`baseCwd`) when it exists on THIS
  // host (code_locality=mount); otherwise the client's own process.cwd() (remote/sealed / an older engine
  // that ships no cwd) — byte-identical fallback. Every relative path arg + execute_command resolves
  // against `base`, so bridged tools target the active project instead of the frozen startup codedir.
  let base = process.cwd();
  let override = false;
  if (baseCwd) {
    try {
      if ((await fs.stat(baseCwd)).isDirectory()) {
        base = baseCwd;
        override = true;
      }
    } catch {
      /* not present on this host → keep process.cwd() (byte-identical fallback) */
    }
  }
  // R resolves a relative path arg against the shipped active-project cwd ONLY when one was shipped AND
  // exists (the mount bridge case); with no override it is the IDENTITY, so every non-bridged caller — and
  // the raw model-supplied path echoed in ERROR/OK strings — stays byte-identical. Callers keep the RAW
  // arg for display and use R(arg) only for the actual fs operation target.
  const R = (v: string): string => (override && !path.isAbsolute(v) ? path.resolve(base, v) : v);
  try {
    const toolName = name === SANDBOXED_EXEC_WIRE_TOOL ? 'execute_command' : name;
    switch (toolName) {
      case 'read_file': {
        const p = R(reqStr(args, 'path'));
        let bounded: {buf: Buffer; size: number; over: boolean};
        try {
          bounded = await readBoundedFile(p, MAX_FILE_BYTES);
        } catch (e) {
          if (errCode(e) === 'ENOENT') return `ERROR: Not found: ${args['path']}`;
          throw e;
        }
        if (bounded.over) {
          return `ERROR: read_file refused: file too large — ${bounded.size} bytes, cap ${MAX_FILE_BYTES} bytes`;
        }
        const text = bounded.buf.toString('utf-8');
        // #1047: a targeted ranged/pattern read returns only the relevant slice; a bad range/pattern
        // falls through to the head+tail cap below (≙ gx10.py `_read_file_ranged`).
        const ranged = rangedRead(text, args);
        if (ranged !== null) return ranged;
        // cap on Unicode CODE POINTS (≙ Python len()/str slicing), not UTF-16 code units.
        const cp = Array.from(text);
        if (cp.length > MAX_FILE_CHARS) {
          const headN = Math.floor((MAX_FILE_CHARS * 2) / 3); // 16000
          const tailN = MAX_FILE_CHARS - headN; // 8000
          const omitted = cp.length - headN - tailN;
          return (
            cp.slice(0, headN).join('') +
            `\n\n... [Ironclad: ${omitted} chars omitted — file ${cp.length} ` +
            `chars, capped at ${MAX_FILE_CHARS}. For targeted excerpts, use search_files to ` +
            `locate the relevant lines, then read only those.] ...\n\n` + // #1047 re-steer (was findstr/Select-String; ≙ gx10.py #1046)
            cp.slice(cp.length - tailN).join('')
          );
        }
        return text;
      }

      case 'write_file': {
        const p = R(reqStr(args, 'path'));
        const content = reqStr(args, 'content');
        await mkdirp(path.dirname(p));
        const tmp = path.join(path.dirname(p), path.basename(p) + '.tmp');
        await fs.writeFile(tmp, content, 'utf-8');
        await fs.rename(tmp, p);
        return `OK: Written ${Array.from(content).length} chars to ${args['path']}`; // code points, ≙ len()
      }

      case 'list_directory': {
        const rawArg = args['path'] === undefined ? '.' : String(args['path']);
        const raw = R(rawArg);                                   // resolved target for readdir/classify/stat
        const entries: Dirent[] = [];
        let overflow = false;
        try {
          const dir = await fs.opendir(raw);
          for await (const entry of dir) {
            entries.push(entry);
            if (entries.length > LIST_DIR_HARD_CAP) {
              overflow = true;
              break;
            }
          }
        } catch (e) {
          if (errCode(e) === 'ENOENT') return `ERROR: Not found: ${pyPathStr(rawArg)}`;   // #1317: raw path, parity
          throw e;
        }
        const total = entries.length;
        // Classify only the bounded snapshot, following symlinks like Python Path.is_dir().
        const dirSet = new Set((await Promise.all(entries.map(async (e) => {
          try {
            return (await fs.stat(path.join(raw, e.name))).isDirectory() ? e.name : null;
          } catch {
            return null;
          }
        }))).filter((name): name is string => name !== null));
        let items = entries.map((e) => ({e, isDir: dirSet.has(e.name)}));
        if (args['sort'] === 'time') {
          const withM = await Promise.all(
            items.map(async (it) => ({...it, m: (await fs.stat(path.join(raw, it.e.name))).mtimeMs})),
          );
          withM.sort((a, b) => b.m - a.m); // desc; ties keep original order (stable)
          items = withM.map(({e, isDir}) => ({e, isDir}));
        } else {
          items = [...items].sort((a, b) => {
            const af = a.isDir ? 0 : 1; // dirs (0) before files (1)
            const bf = b.isDir ? 0 : 1;
            if (af !== bf) return af - bf;
            const an = a.e.name.toLowerCase();
            const bn = b.e.name.toLowerCase();
            return an < bn ? -1 : an > bn ? 1 : 0;
          });
        }
        const lim = pyInt(args['limit']);
        if (lim !== null && lim > 0) items = items.slice(0, lim);
        let capped = false;
        if (items.length > LIST_DIR_HARD_CAP) {
          items = items.slice(0, LIST_DIR_HARD_CAP);
          capped = true;
        }
        const lines = items.map((it) => `${it.isDir ? '[D]' : '[F]'} ${it.e.name}`);
        // #1183: a deterministic count header of the FULL set — LLMs miscount a list (the orchestrator, and
        // me); state the exact numbers so the model reports them verbatim instead of re-counting.
        const nDirs = dirSet.size; // from the SAME snapshot as total → 0 ≤ nDirs ≤ total always
        const count = `${overflow ? 'At least ' : ''}${fmtCount(nDirs, total - nDirs)}`;

        let out = lines.length ? `${count}\n${lines.join('\n')}` : '(empty)';
        const shown = lines.length;
        if (overflow) {
          // #1488 M1: an overflowing dir samples the first LIST_DIR_HARD_CAP entries in filesystem order,
          // so a sort/limit ranks only this partial sample, NOT the true newest across the dir (that needs a
          // full walk — the DoS this cap avoids). Steer to narrowing the path, not to sort='time'.
          const suffix = capped
            ? `; hard cap ${LIST_DIR_HARD_CAP} — narrow the path for a complete listing`
            : ` (limit=${lim === null ? 'None' : lim})`;
          out += `\n... [GX10v3: first ${shown} entries (filesystem order) of many${suffix}; a sort/limit ranks only this partial sample, not the whole directory]`;
        } else if (shown < total) {
          const suffix = capped
            ? ` (hard cap ${LIST_DIR_HARD_CAP} — use sort='time'+limit)`
            : ` (limit=${lim === null ? 'None' : lim})`;
          out += `\n... [GX10v3: showing ${shown} of ${total} entries${suffix}]`;
        }
        return out;
      }

      case 'execute_command': {
        const command = reqStr(args, 'command');
        // #459 (review B S2): the fail-closed guardrail also fires on this LOCAL client path (a bridged
        // command OR the `/sh` escape hatch), so a remote/web fetch or an unbounded/progress process is
        // refused before it can corrupt the renderer. The server-side guard already covers the model's
        // tool calls; this covers what never reaches the server.
        const blocked = shellGuard(command);
        if (blocked !== null) {
          return `BLOCKED: execute_command refuses ${blocked} — it can corrupt the display or hang the session (#459).`;
        }
        // ≙ Python int(args.get("timeout", 30)): absent → 30; present-but-not-int → ERROR
        // (Python raises ValueError/TypeError), NOT a silent 30s fallback that runs the command.
        let timeoutS = 30;
        if (args['timeout'] !== undefined) {
          const t = pyInt(args['timeout']);
          if (t === null) throw new Error(`invalid timeout: ${JSON.stringify(args['timeout'])}`);
          timeoutS = t;
        }
        if (!['auto', 'bwrap', 'firejail'].includes(sandboxPolicy)) {
          return 'ERROR: execute_command refused: sandbox policy must be auto, bwrap, or firejail; Ironclad fails closed.';
        }
        if (process.platform === 'win32') {
          return ('ERROR: execute_command refused: no supported model-command sandbox backend is available on '
            + 'Windows. Ironclad fails closed; use Linux with bwrap/firejail or the separate operator /sh channel.');
        }
        const backend = await findSandboxBackend(sandboxPolicy as SandboxPolicy);
        if (backend === null) {
          return (`ERROR: execute_command refused: no supported sandbox backend is available for policy '${sandboxPolicy}'. `
            + 'Ironclad fails closed; install bwrap or firejail on this Linux host.');
        }
        if (isBestEffortTeardown(backend)) {
          emitDiagnosticOnce(
            'sandbox-firejail-best-effort',
            '⚠ sandbox: firejail tree teardown is best-effort-only; bwrap is preferred for complete namespace teardown',
          );
        }
        let r = await execSandboxed(backend, command, timeoutS, base, signal);
        // #1196 ≙ gx10.py: BSD/macOS `ls` rejects the GNU-only `--color=always` (exit != 0), which would
        // drop the fs-computed header/Answer (gated on exit 0). Retry the LISTING without the colour flag.
        if (r.code !== 0 && command.includes('--color=always') && listingTargetForCommand(command) !== null) {
          r = await execSandboxed(backend, command.replace(/\s*--color=always\b/g, ''), timeoutS, base, signal);
        }
        let out = r.text;
        // #1193/#1195 ≙ gx10.py: a successful simple listing is prepended with the deterministic
        // fs-computed count — the engine-side prepend never runs for a BRIDGED execute_command, so the
        // bridge does it itself. Only on exit 0 with real output (an ERROR/timeout/empty result gets none).
        if (r.code === 0 && out) {
          const target = listingTargetForCommand(command);
          const names = target === null ? null : await directoryEntryNames(R(target));
          if (names !== null) {
            // ONE snapshot feeds header AND answer data (no self-contradicting TOCTOU pair).
            // #1202: the machine AnswerData line is rendered into the localized ready-made `Answer:`
            // sentence SERVER-side (one authoritative language/template/sort) — this bridge ships
            // DATA, never prose. Compact JSON ≙ the engine's json.dumps(separators=(",", ":")).
            const header = fmtCount(names.dirs.length, names.files.length);
            // 200 is the client TRANSPORT bound (don't ship huge name lists over the bridge). It equals
            // the engine's DEFAULT cap; a deployment that raises list_dir_hard_cap ABOVE 200 keeps the
            // ready-made Answer for engine-native listings between 200 and the configured cap but not for
            // bridged ones — those fall back to the header + model prose. A known, documented topology
            // limit under a non-default cap (the count header is unaffected).
            const data =
              names.dirs.length + names.files.length <= LIST_DIR_HARD_CAP
                ? `AnswerData: ${JSON.stringify({dirs: names.dirs, files: names.files})}\n`
                : '';
            out = `${header}\n${data}${out}`;
          }
        }
        return out || `(exit ${r.code ?? 0}, no output)`;
      }

      case 'move_file': {
        const src = R(reqStr(args, 'source'));
        let dst = R(reqStr(args, 'destination'));
        // ≙ shutil.move: destination an existing directory → move INTO it (dst/basename(src)).
        try {
          if ((await fs.stat(dst)).isDirectory()) dst = path.join(dst, path.basename(src));
        } catch {
          /* destination doesn't exist yet → move to that exact path */
        }
        await mkdirp(path.dirname(dst));
        try {
          await fs.rename(src, dst);
        } catch (e) {
          if (errCode(e) === 'EXDEV') {
            await fs.copyFile(src, dst);
            try {
              const st = await fs.stat(src); // ≙ shutil.move's copy2: preserve mtime/atime
              await fs.utimes(dst, st.atime, st.mtime);
            } catch {
              /* best-effort metadata */
            }
            await fs.unlink(src);
          } else throw e;
        }
        return `OK: Moved ${args['source']} → ${args['destination']}`;
      }

      case 'delete_file': {
        await fs.unlink(R(reqStr(args, 'path')));
        return `OK: Deleted ${args['path']}`;
      }

      case 'copy_file': {
        const src = R(reqStr(args, 'source'));
        let dst = R(reqStr(args, 'destination'));
        try {
          await fs.access(src);
        } catch {
          return `ERROR: Source not found: ${args['source']}`;
        }
        // ≙ shutil.copy2: destination an existing directory → copy INTO it (dst/basename(src)).
        try {
          if ((await fs.stat(dst)).isDirectory()) dst = path.join(dst, path.basename(src));
        } catch {
          /* destination doesn't exist yet → copy to that exact path */
        }
        await mkdirp(path.dirname(dst));
        await fs.copyFile(src, dst);
        try {
          const st = await fs.stat(src); // best-effort, ≙ copy2 preserving times
          await fs.utimes(dst, st.atime, st.mtime);
        } catch {
          /* metadata copy is best-effort */
        }
        return `OK: Copied ${args['source']} → ${args['destination']}`;
      }

      case 'search_files': {
        const raw = reqStr(args, 'pattern');
        const directory = R(args['directory'] === undefined ? '.' : String(args['directory']));
        const filePattern = args['file_pattern'] === undefined ? '*.md' : String(args['file_pattern']);
        let hit: (line: string) => boolean;
        try {
          const rx = new RegExp(raw, 'i');
          hit = (line) => rx.test(line);
        } catch (e) {
          if (e instanceof SyntaxError) {
            const needle = raw.toLowerCase();
            hit = (line) => line.toLowerCase().includes(needle);
          } else throw e;
        }
        const hits: string[] = [];
        let filesScanned = 0;
        let byteTruncated = false;
        let budgetTruncated = false;
        for await (const fp of rglob(directory, filePattern)) {
          if (filesScanned >= SEARCH_MAX_FILES) {
            budgetTruncated = true;
            break;
          }
          filesScanned++;
          let bounded: {buf: Buffer; size: number; over: boolean};
          try {
            bounded = await readBoundedFile(fp, SEARCH_MAX_FILE_BYTES, false);
          } catch {
            continue;
          }
          if (bounded.over) byteTruncated = true;
          const lines = pySplitlines(bounded.buf.toString('utf-8'));
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i] as string;
            if (hit(line)) {
              hits.push(`${fp}:${i + 1}: ${line.trim()}`);
              if (hits.length >= SEARCH_HIT_CAP) {
                const notes = [`stopped at the ${SEARCH_HIT_CAP}-hit cap`];
                if (byteTruncated) notes.push(`one or more files exceeded the ${SEARCH_MAX_FILE_BYTES}-byte read cap`);
                return `${hits.join('\n')}\n... [Ironclad: search truncated — ${notes.join('; ')}] ...`;
              }
            }
          }
        }
        const notes: string[] = [];
        if (budgetTruncated) notes.push(`stopped after the ${SEARCH_MAX_FILES}-file scan budget`);
        if (byteTruncated) notes.push(`one or more files exceeded the ${SEARCH_MAX_FILE_BYTES}-byte read cap`);
        let out = hits.length ? hits.join('\n') : 'No matches';
        if (notes.length) out += `\n... [Ironclad: search truncated — ${notes.join('; ')}] ...`;
        return out;
      }

      case 'create_directory': {
        await fs.mkdir(R(reqStr(args, 'path')), {recursive: true});
        return `OK: Created ${args['path']}`;
      }

      default:
        return `ERROR: Unknown tool: ${name}`;
    }
  } catch (e) {
    return `ERROR: ${e instanceof Error ? e.message : String(e)}`;
  }
}
