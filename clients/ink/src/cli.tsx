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
import {Server} from './net/server.js';

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

render(<App srv={srv} codedir={workdir} maxAgents={args.maxAgents} />);
