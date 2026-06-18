/**
 * Phase-2 milestone (LIVE) — the full tool-bridge roundtrip against the Spark: the model
 * calls a local tool, the server emits a \x00TR frame, App.onTool runs runTool on the local
 * fs, and the result is POSTed to /tool-result. Proven by the file landing locally.
 * Gated on GX10_LIVE_URL. Runs in a temp codedir (process.cwd() is what runTool writes to).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {promises as fs} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {render} from 'ink-testing-library';
import {App} from '../src/ui/App.js';
import {Server} from '../src/net/server.js';

const LIVE = process.env['GX10_LIVE_URL'];
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

test('bridge roundtrip — a model write_file lands on the local fs', {skip: !LIVE}, async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-bridge-'));
  const prev = process.cwd();
  process.chdir(dir); // runTool acts on process.cwd()
  const srv = new Server(LIVE as string);
  const {stdin, unmount, lastFrame} = render(<App srv={srv} codedir={dir} maxAgents={3} />);
  try {
    await sleep(400);
    stdin.write('Nutze dein write_file-Werkzeug und erstelle die Datei bridge_ok.txt mit exakt dem Inhalt READY. Keine Rückfrage.');
    await sleep(150); // let React commit setBuffer before Enter
    stdin.write('\r');
    const target = path.join(dir, 'bridge_ok.txt');
    let exists = false;
    for (let i = 0; i < 600 && !exists; i++) {
      await sleep(100);
      try {
        await fs.access(target);
        exists = true;
      } catch {
        /* not yet */
      }
    }
    assert.ok(exists, `bridge_ok.txt created via the tool bridge. last frame:\n${(lastFrame() ?? '').slice(-300)}`);
    assert.equal((await fs.readFile(target, 'utf-8')).trim(), 'READY', 'content written through runTool');
  } finally {
    unmount();
    process.chdir(prev);
    await fs.rm(dir, {recursive: true, force: true});
  }
});
