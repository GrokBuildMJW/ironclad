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
import {promises as fs} from 'node:fs';
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

/** Files under `directory` whose basename matches `filePattern`, depth-first, paths in `str(Path)` form.
 *  The two bounded-memory passes preserve pathlib's current-directory-before-subdirectories order without
 *  accumulating either the full tree or a directory's complete entry list (#1488). */
export async function* rglob(directory: string, filePattern: string): AsyncGenerator<string> {
  const re = fnmatchToRe(filePattern);
  async function* walk(dir: string): AsyncGenerator<string> {
    try {
      const entries = await fs.opendir(dir);
      for await (const e of entries) {
        if (e.isFile() && re.test(e.name)) yield path.join(dir, e.name);
      }
    } catch {
      return; // unreadable dir → skip (≙ rglob silently passing over it)
    }
    // A second lazy pass avoids retaining every subdirectory while keeping the established order.
    try {
      const entries = await fs.opendir(dir);
      for await (const e of entries) {
        if (e.isDirectory()) yield* walk(path.join(dir, e.name));
      }
    } catch {
      return;
    }
  }
  yield* walk(directory);
}
