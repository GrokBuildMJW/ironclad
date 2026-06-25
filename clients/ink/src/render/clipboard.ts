/**
 * clipboard — copy selected text to the OS clipboard via OSC 52 + a native fallback (R6).
 *
 * Since the app owns selection (decision a), it must put text on the clipboard itself. Two paths,
 * used together for reach:
 *  - **OSC 52** (`ESC ] 52 ; c ; <base64> BEL`): the terminal writes its host clipboard. Works over
 *    SSH (no local process needed) and is the primary path.
 *  - **Native command** fallback: `clip` (Windows), `pbcopy` (macOS), `wl-copy`/`xclip`/`xsel`
 *    (Linux) — covers terminals that don't honor OSC 52. Best-effort: a missing tool just falls
 *    through to the next candidate; OSC 52 already covers most cases.
 *
 * The OS spawn is injectable so the policy is unit-testable without touching a real clipboard.
 */
import {spawn} from 'node:child_process';

/** OSC 52 escape that sets the host clipboard to `text` (BEL-terminated). */
export function osc52(text: string): string {
  const b64 = Buffer.from(text, 'utf8').toString('base64');
  return `\x1b]52;c;${b64}\x07`;
}

export interface OsCommand {
  command: string;
  args: string[];
}

/** Candidate native clipboard commands for a platform, in try order (each reads text on stdin). */
export function osClipboardCommands(platform: NodeJS.Platform): OsCommand[] {
  if (platform === 'win32') return [{command: 'clip', args: []}];
  if (platform === 'darwin') return [{command: 'pbcopy', args: []}];
  return [
    {command: 'wl-copy', args: []},
    {command: 'xclip', args: ['-selection', 'clipboard']},
    {command: 'xsel', args: ['--clipboard', '--input']},
  ];
}

/** Default native copy: spawn the first working command for the platform, piping text to stdin. */
export function spawnOsCopy(text: string, platform: NodeJS.Platform): void {
  const candidates = osClipboardCommands(platform);
  const tryAt = (i: number): void => {
    const c = candidates[i];
    if (!c) return; // exhausted — OSC 52 still covers most terminals
    const child = spawn(c.command, c.args, {stdio: ['pipe', 'ignore', 'ignore']});
    child.on('error', () => tryAt(i + 1)); // ENOENT (tool not installed) → next candidate
    child.stdin?.on('error', () => {}); // swallow EPIPE
    child.stdin?.end(text);
  };
  tryAt(0);
}

/** Candidate native clipboard READ commands for a platform, in try order (each writes text to stdout). */
export function osPasteCommands(platform: NodeJS.Platform): OsCommand[] {
  if (platform === 'win32') return [{command: 'powershell', args: ['-NoProfile', '-Command', 'Get-Clipboard -Raw']}];
  if (platform === 'darwin') return [{command: 'pbpaste', args: []}];
  return [
    {command: 'wl-paste', args: ['-n']},
    {command: 'xclip', args: ['-selection', 'clipboard', '-o']},
    {command: 'xsel', args: ['--clipboard', '--output']},
  ];
}

/**
 * Read the OS clipboard asynchronously and hand the text to `onText` (empty string if no tool is
 * available). Async so a slow reader (e.g. PowerShell on Windows) never freezes the render loop.
 */
export function readClipboard(onText: (text: string) => void, platform: NodeJS.Platform = process.platform): void {
  const candidates = osPasteCommands(platform);
  const tryAt = (i: number): void => {
    const c = candidates[i];
    if (!c) {
      onText('');
      return;
    }
    let buf = '';
    const child = spawn(c.command, c.args, {stdio: ['ignore', 'pipe', 'ignore']});
    child.on('error', () => tryAt(i + 1)); // tool not installed → next candidate
    child.stdout?.on('data', (d: Buffer) => {
      buf += d.toString('utf8');
    });
    child.on('close', (code) => {
      if (code === 0 || buf) onText(buf.replace(/\r\n?/g, '\n').replace(/\n+$/, '')); // CRLF + lone CR -> LF (#438)
      else tryAt(i + 1);
    });
  };
  tryAt(0);
}

export interface ClipboardOptions {
  /** Emit OSC 52 to the terminal (default true). */
  osc52?: boolean;
  /** Also run the native clipboard command (default true). */
  osFallback?: boolean;
  /** Platform to target (default `process.platform`). */
  platform?: NodeJS.Platform;
  /** Injectable native-copy implementation (default `spawnOsCopy`). */
  osCopy?: (text: string, platform: NodeJS.Platform) => void;
}

export class Clipboard {
  constructor(
    private readonly write: (data: string) => void,
    private readonly opts: ClipboardOptions = {},
  ) {}

  /** Put `text` on the clipboard via the configured paths. */
  copy(text: string): void {
    if (this.opts.osc52 ?? true) this.write(osc52(text));
    if (this.opts.osFallback ?? true) {
      const run = this.opts.osCopy ?? spawnOsCopy;
      run(text, this.opts.platform ?? process.platform);
    }
  }
}
