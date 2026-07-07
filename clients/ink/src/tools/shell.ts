/**
 * Local shell resolution (#1177, epic #1144).
 *
 * The tool-bridge hard-forced PowerShell on Windows, so a bash command (`ls -la`) couldn't run. Instead pick
 * the shell PER COMMAND: PowerShell cmdlets (`Get-ChildItem`, `Select-String`, `$env:` …) run in PowerShell,
 * everything else in Git Bash when it's installed — so BOTH shells work, neither is forced. This also frees
 * the orchestrator to emit either flavour. Set `GX10_BASH` to a full `bash.exe` path to override detection.
 */
import {existsSync} from 'node:fs';
import {execSync} from 'node:child_process';

export type Shell = 'bash' | 'powershell';

// PowerShell fingerprints: a `Verb-Noun` cmdlet at a command position, or unmistakable PS syntax.
const PS_CMDLET =
  /(?:^|[\s|;&(])(?:Get|Set|New|Remove|Select|Where|ForEach|Write|Add|Copy|Move|Rename|Test|Invoke|Start|Stop|Out|Format|Sort|Measure|Import|Export|ConvertTo|ConvertFrom|Join|Split|Compare|Group|Resolve|Clear|Push|Pop)-[A-Z]\w+/;
const PS_SYNTAX = /\$env:|\$PSItem|\$_(?:\.|\s|\)|$)|-Recurse\b|-Filter\b|-ErrorAction\b|\|\s*(?:Where|Select|ForEach|Sort|Measure)-/i;

/** Which shell a command is written for. Defaults to bash (POSIX-style / shell-agnostic commands). */
export function detectShell(command: string): Shell {
  return PS_CMDLET.test(command) || PS_SYNTAX.test(command) ? 'powershell' : 'bash';
}

/** Pure core: pick a bash executable from the well-known install locations (override + Program Files + Scoop),
 *  given an existence check. `home` is the user profile dir (for the Scoop path). */
export function pickBash(
  platform: NodeJS.Platform,
  exists: (p: string) => boolean,
  override?: string,
  home?: string,
): string | null {
  if (platform !== 'win32') return null; // POSIX runs via the default shell already
  const candidates = [
    override,
    'C:\\Program Files\\Git\\bin\\bash.exe',
    'C:\\Program Files\\Git\\usr\\bin\\bash.exe',
    'C:\\Program Files (x86)\\Git\\bin\\bash.exe',
    home ? `${home}\\scoop\\apps\\git\\current\\bin\\bash.exe` : undefined, // Scoop install
    home ? `${home}\\scoop\\apps\\git\\current\\usr\\bin\\bash.exe` : undefined,
    home ? `${home}\\scoop\\shims\\bash.exe` : undefined,
  ].filter((p): p is string => !!p);
  return candidates.find((p) => exists(p)) ?? null;
}

/** Resolve `bash` on PATH (any install method), skipping WSL's `System32\bash.exe` (runs in the WSL fs). */
function whereBash(): string | null {
  try {
    const out = execSync('where bash', {stdio: ['ignore', 'pipe', 'ignore'], timeout: 3000}).toString();
    const paths = out.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    return paths.find((p) => /bash\.exe$/i.test(p) && !/\\system32\\/i.test(p)) ?? null;
  } catch {
    return null;
  }
}

let _bash: string | null | undefined;

/** The Git Bash executable to prefer on Windows, or null (→ PowerShell / the POSIX default). Cached.
 *  Tries the well-known locations first, then falls back to resolving `bash` on PATH. */
export function gitBash(): string | null {
  if (_bash === undefined) {
    if (process.platform !== 'win32') {
      _bash = null;
    } else {
      _bash =
        pickBash(
          process.platform,
          (p) => {
            try {
              return existsSync(p);
            } catch {
              return false;
            }
          },
          process.env.GX10_BASH,
          process.env.USERPROFILE,
        ) ?? whereBash();
    }
  }
  return _bash;
}

/** The human shell name for a command's tool-call header: the shell it actually runs in. */
export function shellLabel(command: string): string {
  if (process.platform !== 'win32') return 'Bash';
  if (detectShell(command) === 'powershell') return 'PowerShell';
  return gitBash() ? 'Bash' : 'PowerShell'; // a bash command falls back to PowerShell only if no Git Bash
}
