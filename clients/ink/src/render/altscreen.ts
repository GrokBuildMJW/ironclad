/**
 * altscreen — enter/leave the alternate screen + mouse tracking, with clean teardown (R6).
 *
 * The app-owned ScrollBox + selection model (decision a) needs the alternate screen (DEC 1049)
 * and SGR mouse tracking so the app — not the terminal — drives scrolling and selection. The hard
 * requirement is a **clean teardown**: on exit the terminal must be byte-for-byte restored. So
 * `leaveSequence` disables every mode `enterSequence` enabled, in the exact reverse order, and
 * leaves the alternate screen LAST (which also restores the saved cursor and the main buffer).
 *
 * Sequences are plain `\x1b[` strings (no literal control chars in source). The `AltScreen` wrapper
 * makes enter/leave idempotent so a double-enter or a teardown-after-crash can't desync the modes.
 */

const CSI = '\x1b[';

const ALT_ON = CSI + '?1049h'; // alt buffer + save cursor
const ALT_OFF = CSI + '?1049l'; // restore cursor + main buffer
const CURSOR_HIDE = CSI + '?25l';
const CURSOR_SHOW = CSI + '?25h';
const PASTE_ON = CSI + '?2004h'; // bracketed paste
const PASTE_OFF = CSI + '?2004l';

const MOUSE_BASIC = '?1000'; // button press/release
const MOUSE_DRAG = '?1002'; // + report motion while a button is held (selection drag)
const MOUSE_ANY = '?1003'; // + report all motion (hover) — opt-in, noisy
const MOUSE_SGR = '?1006'; // SGR extended coordinates (no 223-column limit)

export interface AltScreenOptions {
  /** Enable SGR mouse tracking (default true). */
  mouse?: boolean;
  /** Use any-motion (1003) instead of drag-only (1002) reporting (default false). */
  anyMotion?: boolean;
  /** Hide the terminal cursor — we draw our own (default true). */
  hideCursor?: boolean;
  /** Enable bracketed paste so a paste arrives as one chunk (default true). */
  bracketedPaste?: boolean;
}

function mouseModes(opts: AltScreenOptions): string[] {
  return [MOUSE_BASIC, opts.anyMotion ? MOUSE_ANY : MOUSE_DRAG, MOUSE_SGR];
}

/** The full sequence to enter the alternate screen + enable the requested input modes. */
export function enterSequence(opts: AltScreenOptions = {}): string {
  const mouse = opts.mouse ?? true;
  const hideCursor = opts.hideCursor ?? true;
  const paste = opts.bracketedPaste ?? true;
  const parts: string[] = [ALT_ON];
  if (hideCursor) parts.push(CURSOR_HIDE);
  if (mouse) for (const m of mouseModes(opts)) parts.push(CSI + m + 'h');
  if (paste) parts.push(PASTE_ON);
  return parts.join('');
}

/** The exact reverse: disable every mode, restore the cursor, and leave the alt screen last. */
export function leaveSequence(opts: AltScreenOptions = {}): string {
  const mouse = opts.mouse ?? true;
  const hideCursor = opts.hideCursor ?? true;
  const paste = opts.bracketedPaste ?? true;
  const parts: string[] = [];
  if (paste) parts.push(PASTE_OFF);
  if (mouse) for (const m of [...mouseModes(opts)].reverse()) parts.push(CSI + m + 'l');
  if (hideCursor) parts.push(CURSOR_SHOW);
  parts.push(ALT_OFF); // last: restores the main buffer + saved cursor
  return parts.join('');
}

/** Idempotent enter/leave around a write sink, so the terminal modes never desync. */
export class AltScreen {
  private active = false;

  constructor(
    private readonly write: (data: string) => void,
    private readonly opts: AltScreenOptions = {},
  ) {}

  get isActive(): boolean {
    return this.active;
  }

  enter(): void {
    if (this.active) return;
    this.write(enterSequence(this.opts));
    this.active = true;
  }

  leave(): void {
    if (!this.active) return;
    this.write(leaveSequence(this.opts));
    this.active = false;
  }
}
