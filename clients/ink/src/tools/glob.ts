/**
 * Recursive glob ≙ Python `Path(directory).rglob(filePattern)`, used by search_files.
 *
 * Critical for wire-parity (gx10.py:1753-1760): the emitted hit prefix `<fp>` must
 * byte-match Python's `str(Path(directory).rglob(...))`. `path.join` uses `path.sep`
 * (`\` on Windows, `/` on POSIX) and collapses `.` exactly as pathlib does, so the
 * relative hit path is identical on both clients — unlike npm `glob`, which emits
 * forward slashes on Windows and would break parity. `filePattern` matches the BASENAME
 * (fnmatch semantics; case-insensitive on Windows, like NTFS / pathlib).
 */
import {promises as fs, type Dirent} from 'node:fs';
import path from 'node:path';

function fnmatchToRe(pattern: string): RegExp {
  let re = '';
  for (const ch of pattern) {
    if (ch === '*') re += '.*';
    else if (ch === '?') re += '.';
    else re += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp('^' + re + '$', process.platform === 'win32' ? 'i' : '');
}

/** Files under `directory` whose basename matches `filePattern`, depth-first, paths in `str(Path)` form. */
export async function rglob(directory: string, filePattern: string): Promise<string[]> {
  const re = fnmatchToRe(filePattern);
  const out: string[] = [];
  async function walk(dir: string): Promise<void> {
    let entries: Dirent[];
    try {
      entries = await fs.readdir(dir, {withFileTypes: true});
    } catch {
      return; // unreadable dir → skip (≙ rglob silently passing over it)
    }
    // Parity (audit): Python Path(dir).rglob emits ALL matching files of the CURRENT
    // directory first, then descends into subdirs — NOT interleaved depth-first. Collect
    // current-dir matches here, recurse afterwards, so hit order (and which 50 survive
    // the hits[:50] cap in search_files) match the Python source byte-for-byte.
    const subdirs: string[] = [];
    for (const e of entries) {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) subdirs.push(full);
      else if (e.isFile() && re.test(e.name)) out.push(full);
    }
    for (const sub of subdirs) await walk(sub);
  }
  await walk(directory);
  return out;
}
