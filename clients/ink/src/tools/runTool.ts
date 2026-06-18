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
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {rglob} from './glob.js';

const MAX_FILE_CHARS = 24000; // gx10.py:102
const LIST_DIR_HARD_CAP = 200; // gx10.py:103

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

/** ≙ subprocess.run with DEVNULL stdin, UTF-8-lossy capture, timeout. */
function execCommand(command: string, timeoutS: number): Promise<string> {
  return new Promise((resolve) => {
    const child =
      process.platform === 'win32'
        ? spawn('powershell', ['-NoProfile', '-NonInteractive', '-Command', command], {
            stdio: ['ignore', 'pipe', 'pipe'],
          })
        : spawn(command, {shell: true, stdio: ['ignore', 'pipe', 'pipe']});
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let done = false;
    const finish = (s: string): void => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(s);
    };
    const timer = setTimeout(() => {
      child.kill();
      finish(`ERROR: Timeout after ${timeoutS}s`);
    }, timeoutS * 1000);
    child.stdout?.on('data', (d: Buffer) => out.push(d));
    child.stderr?.on('data', (d: Buffer) => err.push(d));
    child.on('error', (e) => finish(`ERROR: ${e.message}`));
    child.on('close', (code) => {
      const combined = (Buffer.concat(out).toString('utf-8') + Buffer.concat(err).toString('utf-8')).trim();
      finish(combined || `(exit ${code ?? 0}, no output)`);
    });
  });
}

// ── dispatch ─────────────────────────────────────────────────────────────────
export async function runTool(name: string, args: Args): Promise<string> {
  try {
    switch (name) {
      case 'read_file': {
        const p = reqStr(args, 'path');
        let buf: Buffer;
        try {
          buf = await fs.readFile(p);
        } catch (e) {
          if (errCode(e) === 'ENOENT') return `ERROR: Not found: ${args['path']}`;
          throw e;
        }
        const text = buf.toString('utf-8');
        // cap on Unicode CODE POINTS (≙ Python len()/str slicing), not UTF-16 code units.
        const cp = Array.from(text);
        if (cp.length > MAX_FILE_CHARS) {
          const headN = Math.floor((MAX_FILE_CHARS * 2) / 3); // 16000
          const tailN = MAX_FILE_CHARS - headN; // 8000
          const omitted = cp.length - headN - tailN;
          return (
            cp.slice(0, headN).join('') +
            `\n\n... [Ironclad: ${omitted} chars omitted — file ${cp.length} ` +
            `chars, capped at ${MAX_FILE_CHARS}. For targeted excerpts use ` +
            `execute_command, e.g. findstr/Select-String.] ...\n\n` +
            cp.slice(cp.length - tailN).join('')
          );
        }
        return text;
      }

      case 'write_file': {
        const p = reqStr(args, 'path');
        const content = reqStr(args, 'content');
        await mkdirp(path.dirname(p));
        const tmp = path.join(path.dirname(p), path.basename(p) + '.tmp');
        await fs.writeFile(tmp, content, 'utf-8');
        await fs.rename(tmp, p);
        return `OK: Written ${Array.from(content).length} chars to ${args['path']}`; // code points, ≙ len()
      }

      case 'list_directory': {
        const raw = args['path'] === undefined ? '.' : String(args['path']);
        let entries;
        try {
          entries = await fs.readdir(raw, {withFileTypes: true});
        } catch (e) {
          if (errCode(e) === 'ENOENT') return `ERROR: Not found: ${pyPathStr(raw)}`;
          throw e;
        }
        const total = entries.length;
        let items = entries;
        if (args['sort'] === 'time') {
          const withM = await Promise.all(
            items.map(async (e) => ({e, m: (await fs.stat(path.join(raw, e.name))).mtimeMs})),
          );
          withM.sort((a, b) => b.m - a.m); // desc; ties keep original order (stable)
          items = withM.map((x) => x.e);
        } else {
          items = [...items].sort((a, b) => {
            const af = a.isFile() ? 1 : 0;
            const bf = b.isFile() ? 1 : 0; // dirs (0) before files (1)
            if (af !== bf) return af - bf;
            const an = a.name.toLowerCase();
            const bn = b.name.toLowerCase();
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
        const lines = items.map((i) => `${i.isDirectory() ? '[D]' : '[F]'} ${i.name}`);
        let out = lines.length ? lines.join('\n') : '(empty)';
        const shown = lines.length;
        if (shown < total) {
          const suffix = capped
            ? ` (Hard-Cap ${LIST_DIR_HARD_CAP} — nutze sort='time'+limit)`
            : ` (limit=${lim === null ? 'None' : lim})`;
          out += `\n... [GX10v3: ${shown} von ${total} Einträgen gezeigt${suffix}]`;
        }
        return out;
      }

      case 'execute_command': {
        const command = reqStr(args, 'command');
        // ≙ Python int(args.get("timeout", 30)): absent → 30; present-but-not-int → ERROR
        // (Python raises ValueError/TypeError), NOT a silent 30s fallback that runs the command.
        let timeoutS = 30;
        if (args['timeout'] !== undefined) {
          const t = pyInt(args['timeout']);
          if (t === null) throw new Error(`invalid timeout: ${JSON.stringify(args['timeout'])}`);
          timeoutS = t;
        }
        return await execCommand(command, timeoutS);
      }

      case 'move_file': {
        const src = reqStr(args, 'source');
        let dst = reqStr(args, 'destination');
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
        await fs.unlink(reqStr(args, 'path'));
        return `OK: Deleted ${args['path']}`;
      }

      case 'copy_file': {
        const src = reqStr(args, 'source');
        let dst = reqStr(args, 'destination');
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
        const directory = args['directory'] === undefined ? '.' : String(args['directory']);
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
        for (const fp of await rglob(directory, filePattern)) {
          let content: string;
          try {
            content = (await fs.readFile(fp)).toString('utf-8');
          } catch {
            continue;
          }
          const lines = pySplitlines(content);
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i] as string;
            if (hit(line)) hits.push(`${fp}:${i + 1}: ${line.trim()}`);
          }
        }
        return hits.length ? hits.slice(0, 50).join('\n') : 'No matches';
      }

      case 'create_directory': {
        await fs.mkdir(reqStr(args, 'path'), {recursive: true});
        return `OK: Created ${args['path']}`;
      }

      default:
        return `ERROR: Unknown tool: ${name}`;
    }
  } catch (e) {
    return `ERROR: ${e instanceof Error ? e.message : String(e)}`;
  }
}
