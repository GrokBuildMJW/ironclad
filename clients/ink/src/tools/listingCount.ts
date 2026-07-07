/**
 * Deterministic listing count + ready-made answer (#1193 / #1195 / #1202, epic #1144) ≙ gx10.py
 * `_fmt_count` + `_directory_entry_names` + `_listing_answer_sentence` + `_listing_target_for_command`
 * and friends.
 *
 * A shell listing (`ls` / `dir` / `Get-ChildItem`) carries the same authoritative
 * `N directories, M files` header as `list_directory`, computed from the FILESYSTEM (not by
 * parsing output — brittle across `ls` flags / `Get-ChildItem` / locales), plus a machine
 * `AnswerData: {json}` line the SERVER renders into the final localized `Answer:` reply sentence
 * (#1202) — language, templates, sorting and name-sanitization live engine-side only; this bridge
 * ships DATA. The engine prepends both server-side, but a bridged `execute_command` runs HERE
 * (`X-Local-Tools: 1` routes `LOCAL_TOOL_NAMES` through the client) — without this port neither
 * reaches a bridged setup (#1195). Anything ambiguous (pipes, redirects, subshells, globs,
 * `-R`/recursive, >1 path operand) gets NO header — no guess.
 *
 * Parity limits (same as the rest of this bridge): the tokenizer covers the shlex(posix) subset a
 * listing command uses (quotes + backslash escapes), not the full lexer. (The old
 * Dirent-vs-is_dir symlink limit is CLOSED here: directoryEntryNames follows links via stat.)
 */
import {promises as fs} from 'node:fs';
import path from 'node:path';

/** ≙ gx10._fmt_count: `N directories, M files` with correct singular/plural. */
export function fmtCount(nDirs: number, nFiles: number): string {
  return `${nDirs} director${nDirs === 1 ? 'y' : 'ies'}, ${nFiles} file${nFiles === 1 ? '' : 's'}`;
}

/** ≙ shlex.split(posix=True) for the subset a listing command uses: whitespace-separated words,
 *  single/double quotes, backslash escapes (inside double quotes only `\"` and `\\` escape, as in
 *  Python shlex). Throws on an unbalanced quote / trailing escape (≙ shlex ValueError). */
export function shlexSplit(s: string): string[] {
  const out: string[] = [];
  let cur = '';
  let has = false; // a token exists even when empty (quoted '')
  let i = 0;
  while (i < s.length) {
    const c = s[i] as string;
    if (c === "'") {
      const j = s.indexOf("'", i + 1);
      if (j < 0) throw new Error('No closing quotation');
      cur += s.slice(i + 1, j);
      has = true;
      i = j + 1;
    } else if (c === '"') {
      has = true;
      i++;
      let closed = false;
      while (i < s.length) {
        const d = s[i] as string;
        if (d === '"') {
          closed = true;
          i++;
          break;
        }
        if (d === '\\' && (s[i + 1] === '"' || s[i + 1] === '\\')) {
          cur += s[i + 1];
          i += 2;
        } else {
          cur += d;
          i++;
        }
      }
      if (!closed) throw new Error('No closing quotation');
    } else if (c === '\\') {
      if (i + 1 >= s.length) throw new Error('No escaped character');
      cur += s[i + 1];
      has = true;
      i += 2;
    } else if (c === ' ' || c === '\t' || c === '\r' || c === '\n') {
      // exactly shlex.whitespace — JS \s would also split NBSP/VT/FF/U+3000 etc., where the
      // engine keeps one token (not a listing verb) and yields no header
      if (has) {
        out.push(cur);
        cur = '';
        has = false;
      }
      i++;
    } else {
      cur += c;
      has = true;
      i++;
    }
  }
  if (has) out.push(cur);
  return out;
}

import type {Dirent} from 'node:fs';

/** True iff a directory entry resolves to a directory, FOLLOWING a symlink/junction via stat — matching
 *  Python's Path.is_dir(). A broken link → false (≙ Python). */
async function entryIsDir(dir: string, e: Dirent): Promise<boolean> {
  if (e.isDirectory()) return true;
  if (e.isSymbolicLink()) {
    try {
      return (await fs.stat(path.join(dir, e.name))).isDirectory();
    } catch {
      return false; // broken link → not a directory
    }
  }
  return false;
}

/** ≙ gx10._directory_entry_names: classify ALREADY-READ dirents of *dir* into {dirs, files} names
 *  (symlink-following). Takes the caller's single readdir snapshot so a listing has ONE snapshot feed
 *  its count, markers AND classification — no TOCTOU pair. */
export async function classifyEntries(dir: string, entries: Dirent[]): Promise<{dirs: string[]; files: string[]}> {
  const dirs: string[] = [];
  const files: string[] = [];
  for (const e of entries) (await entryIsDir(dir, e) ? dirs : files).push(e.name);
  return {dirs, files};
}

/** ≙ gx10._directory_entry_names: the {dirs, files} names of a directory (same hidden-entry policy
 *  as list_directory) — null if the path is not a readable directory. A symlink/junction to a
 *  directory is FOLLOWED, matching Python's Path.is_dir(). ONE readdir snapshot. */
export async function directoryEntryNames(p: string): Promise<{dirs: string[]; files: string[]} | null> {
  let entries;
  try {
    entries = await fs.readdir(p, {withFileTypes: true});
  } catch {
    return null; // missing / not a directory / unreadable ≙ the Python OSError → None
  }
  return classifyEntries(p, entries);
}

/** ≙ gx10._directory_count_header: deterministic `N directories, M files` for a directory —
 *  null if the path is not a readable directory. */
export async function directoryCountHeader(p: string): Promise<string | null> {
  const names = await directoryEntryNames(p);
  return names === null ? null : fmtCount(names.dirs.length, names.files.length);
}


/** ≙ ntpath.join for the one case that differs: a Windows drive-relative operand (`C:temp`) is
 *  returned unchanged (it resolves against that drive's own cwd, exactly like Python) — gluing it
 *  under `base` would manufacture an unreadable `base\C:temp` path and lose the header.
 *  `platform` is injectable (≙ shell.ts pickBash) so the semantics are unit-testable on every OS. */
export function joinLikePython(base: string, p: string, platform: NodeJS.Platform = process.platform): string {
  if (platform === 'win32' && /^[A-Za-z]:/.test(p)) return p;
  return path.join(base, p);
}

const LISTING_VERBS = new Set(['ls', 'dir', 'get-childitem', 'gci']);

/** ≙ gx10._listing_count_header_for_command: the deterministic count header for a SIMPLE listing
 *  command — see listingTargetForCommand. */
export async function listingCountHeaderForCommand(command: string): Promise<string | null> {
  const target = listingTargetForCommand(command);
  return target === null ? null : directoryCountHeader(target);
}

/** ≙ gx10._listing_target_for_command: if *command* is a SIMPLE directory listing
 *  (`ls`/`dir`/`Get-ChildItem`, optionally one leading `cd <path> &&`), return its resolved target
 *  directory path. null (no guess) for anything ambiguous: pipes, redirects, subshells, globs,
 *  `-R`/recursive, or more than one path operand. */
export function listingTargetForCommand(command: string): string | null {
  const cmd = (command ?? '').trim();
  if (!cmd || cmd.includes('||')) return null;
  for (const t of ['|', '>', '<', '$(', '`', ';', '\n']) if (cmd.includes(t)) return null;
  const parts = cmd.split('&&');
  if (parts.length > 2) return null;
  let base = process.cwd(); // ≙ _exec_cwd() or os.getcwd() — the bridge always runs in --codedir
  if (parts.length === 2) {
    let cdTok: string[];
    try {
      cdTok = shlexSplit((parts[0] as string).trim());
    } catch {
      return null;
    }
    if (cdTok.length !== 2 || cdTok[0] !== 'cd') return null;
    const dir = cdTok[1] as string;
    base = path.isAbsolute(dir) ? dir : joinLikePython(base, dir);
  }
  const listPart = (parts[parts.length - 1] as string).trim();
  if (listPart.includes('&')) return null; // a stray background '&'
  let tok: string[];
  try {
    tok = shlexSplit(listPart);
  } catch {
    return null;
  }
  if (!tok.length || !LISTING_VERBS.has((tok[0] as string).toLowerCase())) return null;
  // ≙ gx10: PowerShell cmdlets are case-INSENSITIVE and take VALUE-bearing named params whose value
  // would be misread as the path operand, so a PS-style listing with ANY named parameter is ambiguous
  // → no header. ls/dir keep clustered short flags (R = recursive is uppercase, bash-specific).
  const psStyle = ['get-childitem', 'gci'].includes((tok[0] as string).toLowerCase());
  for (const t of tok.slice(1)) {
    if (!t.startsWith('-')) continue;
    const low = t.toLowerCase();
    if (low === '--recursive' || low === '-recurse' || low === '-r' || (!t.startsWith('--') && t.includes('R'))) {
      return null; // recursive (any case) — the header would no longer describe ONE directory
    }
    if (psStyle) return null; // a named parameter on a PS cmdlet takes a value → ambiguous target
  }
  const operands = tok.slice(1).filter((t) => !t.startsWith('-'));
  if (operands.length > 1) return null;
  let target = operands.length ? (operands[0] as string) : '.';
  for (const ch of '*?[]{}<>|;$`') if (target.includes(ch)) return null;
  if (!path.isAbsolute(target)) target = joinLikePython(base, target);
  return target;
}
