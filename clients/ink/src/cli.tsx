#!/usr/bin/env node
/**
 * Ironclad CLI — Node front-end entrypoint, on our custom renderer.
 *
 * Renders the React UI through our purpose-built renderer (`render` = `mount`, replacing Ink):
 * a packed-cell buffer + cell diff for ghost-free resize and smooth streaming, on the alternate
 * screen with app-owned scrollback + selection + copy (OSC 52 / native). This client talks ONLY
 * to the Python orchestrator over HTTP; the Spark core is untouched. argparse parity:
 * --server / --codedir / --max-agents.
 */
import React from 'react';
import {chdir} from 'node:process';
import {resolve} from 'node:path';
import {execSync} from 'node:child_process';
import {render} from './render/ink-compat.js';
import {App} from './ui/App.js';
import {loadConfig, parseArgs} from './config.js';
import {load as loadSession, exitMessage, statePath} from './state/persist.js';
import {Server} from './net/server.js';
import {establishSession} from './net/session.js';
import {reapCoders} from './agent/handover.js';

// #1541: how long to let a just-killed coder's /feedback + /unclaim POST settle under the still-open session
// before we close it. Bounded so exit stays prompt even if the server is slow.
const CODER_DRAIN_MS = 5000;

// The Windows console defaults to a non-UTF-8 OEM code page, which renders our UTF-8 output
// (box-drawing borders, ◆/●/○ status dots, the █▚▞█ banner, …/≤) as cp1252 mojibake. Force the
// console to UTF-8 so the renderer's glyphs display correctly. Best-effort and Windows-only.
if (process.platform === 'win32') {
  try {
    execSync('chcp 65001', {stdio: 'ignore'});
  } catch {
    /* non-console hosts (pipes, CI) ignore this */
  }
}

const cfg = loadConfig();
const args = parseArgs(process.argv.slice(2), cfg);
// Resolve the working directory to an ABSOLUTE path and chdir there: the passed-through code-tools
// run relative to process.cwd(). Showing the absolute path in the header (below) is ground truth —
// it's the actual directory the local tools act on, independent of anything the model "remembers".
const workdir = resolve(args.codedir);
chdir(workdir);
const srv = new Server(args.server, {token: cfg.serverToken});

// INK-SESSION (#503): mirror client.py — under the sealed profile every gated route needs an
// X-Session-Id, so open a Phase-d session + heartbeat BEFORE the first turn (else each turn and the 2s
// status poll 401). No-op + fail-soft on the open/token profile or an unreachable server. Done before
// render so the very first turn is already authorized.
const session = await establishSession(srv, (m) => process.stdout.write(`${m}\n`));

const app = render(<App srv={srv} codedir={workdir} maxAgents={args.maxAgents} resume={args.resume} />);

// MEM-18: on exit (after the TUI tears down), tell the user the session is saved + how to resume —
// like other code CLIs. Only when there's a non-empty saved session. MEM-19: read this project's
// per-directory state file (<codedir>/.ironclad-cli/session.json).
void app.waitUntilExit().then(async () => {
  // #1541: reap any in-flight /work or /auto coder BEFORE closing the session — kill the child(ren) and await
  // their processOne cleanup (bounded) so their /unclaim (and any completed /feedback) POST while the session
  // is still open; otherwise an orphaned coder finishing after session.stop() 401s and leaves the task stuck.
  await reapCoders(CODER_DRAIN_MS);
  await session.stop(); // INK-SESSION (#503): heartbeat off + session closed on exit (fail-soft)
  const msg = exitMessage(loadSession(statePath(workdir)));
  if (msg) process.stdout.write(`\n${msg}\n`);
});
