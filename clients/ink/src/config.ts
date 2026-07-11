/**
 * Client configuration — secret-free, with the documented precedence
 * **config file < `GX10_*` env < CLI flags** (later wins). No Spark IP / host / vessel
 * literal ever appears here; the optional config file lives on the user's machine.
 *
 * The config file is JSON at `$GX10_CONFIG`, else the OS user-config dir
 * (`%APPDATA%\ironclad\config.json` on Windows, `~/.config/ironclad/config.json` elsewhere).
 * It is optional and fail-soft: missing or malformed → defaults, never a crash.
 */
import {readFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {join} from 'node:path';

/** Client version shown in the header — keep in sync with package.json's "version". */
export const VERSION = '0.1.0';

export interface Config {
  serverUrl: string;
  serverToken: string | null;
  tunnelCmd: string | null;
  claudeBin: string;
  claudeEffort: string;
  claudePermissionMode: string;
  agentCmd: string;
  // INK-HANDOVER-1 (#503): the EXPLICIT client-side bin/template (env or config file), null if unset.
  // Distinct from the resolved claudeBin/agentCmd above: an explicit override beats the server's
  // per-agent spec, whereas an unset value lets the server spec win (then the built-in default).
  claudeBinExplicit: string | null;
  agentCmdExplicit: string | null;
  maxAgents: number;
  srcDir: string | null; // MEM-17: repo root for /update (rebuild+reinstall); null = unknown
}

/** Shape of the optional JSON config file (every field optional). */
interface FileConfig {
  serverUrl?: string;
  serverToken?: string | null;
  tunnelCmd?: string | null;
  claudeBin?: string;
  claudeEffort?: string;
  claudePermissionMode?: string;
  agentCmd?: string;
  maxAgents?: number;
  srcDir?: string | null;
}

/** Resolve the config-file path: `$GX10_CONFIG`, else the OS user-config dir. */
export function configPath(): string {
  const explicit = process.env['GX10_CONFIG'];
  if (explicit) return explicit;
  const base =
    process.platform === 'win32'
      ? process.env['APPDATA'] ?? join(homedir(), 'AppData', 'Roaming')
      : process.env['XDG_CONFIG_HOME'] ?? join(homedir(), '.config');
  return join(base, 'ironclad', 'config.json');
}

/** Read the JSON config file if present. Missing/unreadable/malformed → {} (never throws). */
function readFileConfig(): FileConfig {
  let text: string;
  try {
    text = readFileSync(configPath(), 'utf8');
  } catch {
    return {}; // not present / unreadable → defaults
  }
  try {
    const obj: unknown = JSON.parse(text);
    return obj && typeof obj === 'object' ? (obj as FileConfig) : {};
  } catch {
    return {}; // malformed JSON → ignore, don't crash the client
  }
}

export function loadConfig(): Config {
  const f = readFileConfig(); // precedence below: file value < env (env wins)
  const str = (name: string, fileVal: string | undefined, dflt: string): string =>
    process.env[name] ?? fileVal ?? dflt;
  const opt = (name: string, fileVal: string | null | undefined): string | null =>
    process.env[name] || (fileVal ?? null);
  return {
    serverUrl: str('GX10_SERVER_URL', f.serverUrl, 'http://localhost:8100'),
    serverToken: opt('GX10_SERVER_TOKEN', f.serverToken),
    tunnelCmd: opt('GX10_TUNNEL_CMD', f.tunnelCmd),
    claudeBin: str('GX10_CLAUDE_BIN', f.claudeBin, 'claude'),
    claudeEffort: str('GX10_CLAUDE_EFFORT', f.claudeEffort, 'high'),
    claudePermissionMode: str('GX10_CLAUDE_PERMISSION_MODE', f.claudePermissionMode, 'bypassPermissions'),
    agentCmd: str(
      'GX10_AGENT_CMD',
      f.agentCmd,
      '{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}',
    ),
    claudeBinExplicit: opt('GX10_CLAUDE_BIN', f.claudeBin), // INK-HANDOVER-1: env|file, else null
    agentCmdExplicit: opt('GX10_AGENT_CMD', f.agentCmd),
    maxAgents: parseInt(process.env['GX10_MAX_AGENTS'] ?? '', 10) || f.maxAgents || 3,
    srcDir: opt('GX10_SRC', f.srcDir),
  };
}

export interface Args {
  server: string;
  codedir: string;
  maxAgents: number;
  resume: boolean; // MEM-14: opt into resuming the persisted session (default = fresh start)
}

const _truthy = (v: string | undefined): boolean =>
  (v ?? '').trim().toLowerCase() === '1' ||
  (v ?? '').trim().toLowerCase() === 'true' ||
  (v ?? '').trim().toLowerCase() === 'yes' ||
  (v ?? '').trim().toLowerCase() === 'on';

/** argparse parity: --server (default GX10_SERVER_URL), --codedir (default .), --max-agents.
 *  MEM-14: resume is OPT-IN — default is a fresh start. `--resume` / env `GX10_RESUME` turn it on;
 *  `--fresh` / `--no-resume` / `GX10_NO_RESUME` force it off (GX10_NO_RESUME wins over GX10_RESUME). */
export function parseArgs(argv: string[], cfg: Config): Args {
  const a: Args = {
    server: cfg.serverUrl,
    codedir: '.',
    maxAgents: cfg.maxAgents,
    resume: _truthy(process.env['GX10_RESUME']) && !_truthy(process.env['GX10_NO_RESUME']),
  };
  for (let i = 0; i < argv.length; i++) {
    const t = argv[i];
    if (t === '--server') a.server = argv[++i] ?? a.server;
    else if (t === '--codedir') a.codedir = argv[++i] ?? a.codedir;
    else if (t === '--max-agents') a.maxAgents = parseInt(argv[++i] ?? '', 10) || a.maxAgents;
    else if (t === '--resume') a.resume = true;
    else if (t === '--fresh' || t === '--no-resume') a.resume = false;
    else if (t?.startsWith('--server=')) a.server = t.slice('--server='.length);
    else if (t?.startsWith('--codedir=')) a.codedir = t.slice('--codedir='.length);
    else if (t?.startsWith('--max-agents=')) a.maxAgents = parseInt(t.slice('--max-agents='.length), 10) || a.maxAgents;
  }
  return a;
}
