/**
 * Root Ink component (Phase 1 MVP chat).
 *
 * Layout: a <Static> transcript (committed once → native terminal scrollback, so mouse
 * select/copy/scroll stay native) + a live tail (input box, or the working line while a
 * turn streams) + the pinned status footer. A turn streams via chatStream → router →
 * one committed Markdown block (render-once; live token markdown is Phase 3).
 *
 * Tool-bridge frames run the real local tools via runPassthroughTool (Phase 2): execute on
 * the local fs (process.cwd() = --codedir), POST the result back to /tool-result.
 */
import React, {useEffect, useMemo, useRef, useState, type ReactNode} from 'react';
import {Box, Spacer, Static, Text, useApp, useInput, useStdout} from '../render/ink-compat.js';
import type {Server} from '../net/server.js';
import {classify, completions, catalogueToCommands, HELP_TEXT, type Command} from '../commands.js';
import {chatStream} from '../net/stream.js';
import {answerBody, createRouter} from '../stream/route.js';
import {renderMarkdown, StreamMarkdown} from '../markdown.js';
import {useStatusPoller} from './useStatusPoller.js';
import {
  newPasteStore, isMultilinePaste, storePaste, expandPastes, stripSentinels,
  backspace as backspacePaste,
} from './pasteStore.js';
import {Footer} from './Footer.js';
import {WorkingLine} from './WorkingLine.js';
import {InputBox} from './InputBox.js';
import {CommandMenu} from './CommandMenu.js';
import {menuKey, completionText} from './menuModel.js';
import {runPassthroughTool} from '../tools/bridge.js';
import {runTool} from '../tools/runTool.js';
import {runUpdate} from '../tools/update.js';
import {Pool, dispatchPending, type HandoverCfg} from '../agent/handover.js';
import {loadConfig, VERSION} from '../config.js';
import {load as loadSession, save as saveSession, clear as clearSession, transcriptStats, statePath} from '../state/persist.js';
import {ACCENT, DIM, ERROR, TEXT, VERBS} from './theme.js';

interface Item {
  id: number;
  node: ReactNode;
}

export function App({
  srv,
  codedir,
  maxAgents,
  resume = false,
}: {
  srv: Server;
  codedir: string;
  maxAgents: number;
  resume?: boolean; // MEM-14: opt into resuming the persisted session (default = fresh)
}): React.ReactElement {
  const {exit} = useApp();
  const {stdout} = useStdout();
  const width = stdout?.columns ?? 80;
  const [status, setPerf, setAgent, setSearch] = useStatusPoller(srv);
  const [items, setItems] = useState<Item[]>([]);
  const [buffer, setBuffer] = useState('');
  const [menuSel, setMenuSel] = useState(0); // MEM-16(2): selected slash-command suggestion
  const [menuDismissed, setMenuDismissed] = useState(false); // Esc closes the menu until buffer changes
  const [catalogueCmds, setCatalogueCmds] = useState<Command[]>([]); // #149: server-fed prompt items
  const catalogueFetched = useRef(false); // fetch the /catalogue once, lazily, on first slash menu
  const [thinking, setThinking] = useState(false);
  const [liveAnswer, setLiveAnswer] = useState(''); // streamed markdown preview while a turn runs
  const [frame, setFrame] = useState(0);
  const [secs, setSecs] = useState(0);
  const [tokens, setTokens] = useState(0);
  const verbRef = useRef<string>(VERBS[0]);
  const idRef = useRef(0);
  const t0Ref = useRef(0);
  const didInit = useRef(false);
  const transcriptRef = useRef<string[]>([]); // §3b(a): plain-text transcript persisted for resume
  const pasteStoreRef = useRef(newPasteStore()); // #438: per-turn multi-line pastes, shown collapsed as [Pasted #N +L lines]
  const sessionFile = useMemo(() => statePath(codedir), [codedir]); // MEM-19: per-project state path
  const srcDir = useMemo(() => loadConfig().srcDir, []); // MEM-17: repo root for /update (GX10_SRC)
  const poolRef = useRef<Pool | null>(null);
  const claimedRef = useRef<Set<string>>(new Set());
  const autoRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null); // aborts the in-flight streaming turn
  const hcfg = useMemo<HandoverCfg>(() => {
    const c = loadConfig();
    return {
      claudeBin: c.claudeBin,
      claudeEffort: c.claudeEffort,
      claudePermissionMode: c.claudePermissionMode,
      agentCmd: c.agentCmd,
    };
  }, []);

  const commit = (node: ReactNode): void => {
    const id = idRef.current++;
    setItems((xs) => [...xs, {id, node}]);
  };

  // §3b(a): persist the (text) transcript + non-secret session handle so a Spark restart / vLLM
  // reload doesn't lose the session. Fail-soft; the token is never written.
  const persist = (): void =>
    saveSession(
      {
        serverUrl: srv.base,
        codedir,
        sessionId: srv.sessionId,
        transcript: transcriptRef.current,
        updatedAt: Date.now(),
      },
      sessionFile,
    );

  // MEM-14/MEM-19: restore the saved session into the scrollback. The state file is now per-project
  // (sessionFile = <codedir>/.ironclad-cli/…), so the codedir already matches by construction — we
  // only guard on serverUrl (don't replay a transcript captured against a different backend). Used
  // by the opt-in auto-resume (--resume) and the in-session /resume.
  const resumeSession = (): boolean => {
    const prev = loadSession(sessionFile);
    if (prev && prev.serverUrl === srv.base && prev.transcript.length) {
      transcriptRef.current = [...prev.transcript];
      const {turns, lines} = transcriptStats(prev.transcript);
      commit(<Text color={DIM}>{`  ↻ ${turns} Turn(s) (${lines} Zeilen) wiederhergestellt`}</Text>);
      prev.transcript.forEach((line) => commit(<Text color={DIM}>{line}</Text>));
      return true;
    }
    return false;
  };

  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;
    commit(
      <Text bold color={ACCENT}>
        █▀▄▀█ Ironclad <Text color={DIM}>· Orchestrator Client</Text>
      </Text>,
    );
    commit(<Text color={DIM}>{`  Ironclad CLI ${VERSION} · code ${codedir} · ≤${maxAgents} agents`}</Text>);
    commit(<Text color={DIM}> /help · exit</Text>);
    // MEM-14/MEM-18: resume is OPT-IN (default = fresh). With --resume we restore now; otherwise we
    // start clean and stay quiet — the saved session is kept on disk (persist keeps running) and the
    // goodbye on exit (cli.tsx) tells the user it can be brought back with /resume. No startup hint.
    if (resume && !resumeSession()) {
      commit(<Text color={DIM}> (keine gespeicherte Sitzung zum Wiederherstellen)</Text>);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // stop the /auto poller on unmount
  useEffect(
    () => () => {
      if (autoRef.current) clearInterval(autoRef.current);
    },
    [],
  );

  // spinner + elapsed while a turn streams
  useEffect(() => {
    if (!thinking) return;
    const id = setInterval(() => {
      setFrame((f) => f + 1);
      setSecs(Math.floor((Date.now() - t0Ref.current) / 1000));
    }, 120);
    return () => clearInterval(id);
  }, [thinking]);

  async function streamTurn(payload: string, conversational = true): Promise<void> {
    commit(
      <Box marginTop={1}>
        <Text color={DIM}>{`> ${payload}`}</Text>
      </Box>,
    );
    // MEM-11: only real conversational turns are kept (persisted + summarised). Slash commands
    // (/status, /clear, /config, /ls, …) are repeatable — show them, but don't record them.
    if (conversational) transcriptRef.current.push(`> ${payload}`);
    const idx = Math.floor(Date.now() / 137) % VERBS.length;
    verbRef.current = VERBS[idx] ?? 'Working';
    t0Ref.current = Date.now();
    setSecs(0);
    setTokens(0);
    setThinking(true);
    setAgent(''); // #453: clear last turn's coder so a non-routed turn shows no stale "live" indicator
    setSearch(''); // S9: clear last turn's web-search summary
    setLiveAnswer('');
    const stream = new StreamMarkdown(Math.max(20, width - 4));
    let lastRender = 0;
    const router = createRouter();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      await chatStream(
        srv,
        payload,
        {
          onText: (c) => {
            router.feed(c);
            if (router.tokens) setTokens(router.tokens);
            if (router.perf) setPerf(router.perf);
            if (router.agent) setAgent(router.agent); // #453: live "which coder" indicator
            if (router.search) setSearch(router.search); // S9: live web-search summary
            // live preview, throttled to ~30fps so a fast stream doesn't re-render every token
            const now = Date.now();
            if (now - lastRender > 33) {
              lastRender = now;
              setLiveAnswer(stream.render(answerBody(router)));
            }
          },
          onTool: (f) => runPassthroughTool(srv, f),
        },
        ac.signal,
      );
    } catch (e) {
      // a user-initiated abort (Esc / Ctrl+C) is expected — note it quietly, don't show a ✗ error
      if (ac.signal.aborted) commit(<Text color={DIM}> abgebrochen</Text>);
      else commit(<Text color={ERROR}>{`  ✗ ${String(e)}`}</Text>);
    } finally {
      abortRef.current = null;
    }
    router.flush();
    if (router.perf) setPerf(router.perf);
    if (router.agent) setAgent(router.agent); // #453
    if (router.search) setSearch(router.search); // S9
    setLiveAnswer(''); // drop the live preview; the exact whole-document render is committed below
    const body = answerBody(router);
    if (body) {
      const md = renderMarkdown(body, Math.max(20, width - 4));
      commit(
        <Box paddingLeft={2} marginTop={1}>
          <Text>{md}</Text>
        </Box>,
      );
      if (conversational) transcriptRef.current.push(body); // §3b(a)/MEM-11: only real turns
    }
    if (conversational) persist(); // §3b(a): snapshot transcript + session handle (fail-soft, no token)
    setThinking(false);
  }

  /** Abort the running turn (Esc / Ctrl+C): stop awaiting the stream locally — back to idle at once
   *  — and tell the server to stop generating. Never blocks on the server (a thinking-runaway may
   *  not yield for a while); the local abort is what makes the cancel feel instant. */
  function cancelTurn(): void {
    abortRef.current?.abort();
    void srv.cancel().catch(() => ({}));
  }

  async function handleLocal(name: string, payload: string): Promise<void> {
    const hlog = (m: string): void => commit(<Text color={DIM}>{m}</Text>);
    try {
      if (name === 'help') commit(<Text color={TEXT}>{HELP_TEXT}</Text>);
      else if (name === 'sh') {
        // MEM-15: run a shell command locally (PowerShell on Windows) in the codedir — no server
        // turn, not persisted. Output to the transcript. (-NonInteractive: interactive cmds time out.)
        if (!payload) commit(<Text color={DIM}> (leerer Befehl)</Text>);
        else {
          commit(<Box marginTop={1}><Text color={DIM}>{`! ${payload}`}</Text></Box>);
          const out = await runTool('execute_command', {command: payload});
          commit(<Box paddingLeft={2}><Text>{out}</Text></Box>);
        }
      } else if (name === 'reset') {
        // MEM-12: one button for "answers got weird → start clean". Clears all HOT+WARM layers —
        // client transcript + persisted session + server context (clear_context, which also drops
        // the warm rolling summary). Cold/Mem0 (long-term knowledge) is kept.
        setItems([]);
        transcriptRef.current = [];
        clearSession(sessionFile);
        try {
          await srv.chat('clear'); // → server _dispatch → clear_context() + warm summary clear
        } catch {
          /* server unreachable → local reset still done */
        }
        commit(<Text color={DIM}> ↺ Sitzung zurückgesetzt (Transkript + Kontext + Summary geleert; Langzeit-Memory bleibt)</Text>);
      } else if (name === 'resume') {
        // MEM-14: on-demand restore of the saved session (default start is fresh).
        if (!resumeSession()) commit(<Text color={DIM}> (keine gespeicherte Sitzung)</Text>);
      } else if (name === 'update') {
        // MEM-17: rebuild + reinstall the global `ironclad` from source (GX10_SRC) — no manual
        // `cd clients/ink && …`. The Node process can't hot-swap itself, so it stages the build and
        // asks for a restart. `/update pull` does a git pull first.
        if (!srcDir) {
          commit(<Text color={ERROR}> /update braucht den Quellpfad — setze GX10_SRC (Repo-Wurzel) oder srcDir in config.json.</Text>);
        } else {
          const pull = (payload.split(/\s+/)[1] ?? '').toLowerCase() === 'pull';
          commit(<Text color={DIM}>{`  /update — baue + installiere aus ${srcDir}${pull ? ' (mit git pull)' : ''} …`}</Text>);
          const {ok, log} = await runUpdate(srcDir, pull);
          log.forEach((l) => commit(<Text color={ok ? DIM : ERROR}>{`  ${l}`}</Text>));
        }
      } else if (name === 'health') commit(<Text color={DIM}>{`  ${JSON.stringify(await srv.health())}`}</Text>);
      else if (name === 'tasks') {
        const ts = await srv.tasks();
        if (!ts.length) commit(<Text color={DIM}> (no tasks)</Text>);
        ts.forEach((t) =>
          commit(<Text>{`  ${String(t['status'] ?? '?')}  ${String(t['id'] ?? '?')}  ${String(t['title'] ?? '')}`}</Text>),
        );
      } else if (name === 'pending') {
        const ps = await srv.pending();
        if (!ps.length) commit(<Text color={DIM}> (no open handovers)</Text>);
        ps.forEach((p) => commit(<Text>{`  ${String(p['id'] ?? '?')}  ${String(p['title'] ?? '')}`}</Text>));
      } else if (name === 'coders') {
        // #452: which coding agents are bound (● green) vs not found (○ red), then the fan-out lane.
        // #454: `/coders use <id>|auto` pins/clears the runtime coding agent.
        const parts = payload.split(/\s+/);
        if (parts.length >= 2 && parts[1]?.toLowerCase() === 'use') {
          const res = await srv.setCoderPin(parts[2] ?? 'auto');
          const pin = res['pinned'];
          commit(
            <Text color="cyan">
              {pin ? `  → pinned coder: ${String(pin)}` : '  → coder pin cleared (auto: the staged agent per task)'}
            </Text>,
          );
        }
        const d = await srv.coders();
        const coding = (d['coding_agents'] as Array<Record<string, unknown>>) ?? [];
        const pinned = d['pinned'] ? String(d['pinned']) : '';
        commit(
          <Text color={DIM}>
            {pinned ? `  pinned: ${pinned}  (/coders use auto to clear)` : '  routing: auto (orchestrator staged agent)'}
          </Text>,
        );
        if (!coding.length) commit(<Text color={DIM}> (no coding agents configured)</Text>);
        coding.forEach((a) => {
          // #460: an onboarded-but-disabled agent (enabled:false, e.g. KIMI pending calibration) is inert
          // but shown as registered. `enabled` is absent on older servers → default true (back-compat).
          const enabled = a['enabled'] !== false;
          const bound = Boolean(a['bound']);
          const isPin = pinned && String(a['id'] ?? '').toUpperCase() === pinned.toUpperCase();
          const dot = !enabled ? <Text color={DIM}>◌</Text> : <Text color={bound ? 'green' : 'red'}>{bound ? '●' : '○'}</Text>;
          let suffix: React.ReactNode = '';
          if (!enabled) suffix = <Text color={DIM}>{'  (onboarded · disabled)'}</Text>;
          else if (isPin) suffix = <Text color="cyan">{'  ← pinned'}</Text>;
          else if (!bound) suffix = <Text color={DIM}>{'  (binary not found)'}</Text>;
          commit(
            <Text>
              {'  '}
              {dot}
              {`  ${String(a['id'] ?? '?').padEnd(8)} ${String(a['model'] ?? '—')}`}
              {suffix}
            </Text>,
          );
        });
        const prov = (d['providers'] as Record<string, unknown>) ?? {};
        const pool = (prov['pool'] as Array<Record<string, unknown>>) ?? [];
        if (pool.length) {
          const b = (prov['budget'] as Record<string, unknown>) ?? {};
          const spent = Number(b['spent_usd'] ?? 0).toFixed(4);
          commit(
            <Text color={DIM}>{`  providers (fan-out): ${prov['active'] ? 'active' : 'inactive'} · spent $${spent}`}</Text>,
          );
          pool.forEach((p) => {
            const reach = Boolean(p['reachable']);
            const reason = p['last_route_reason'] ? `  ← ${String(p['last_route_reason'])}` : '';
            commit(
              <Text>
                {'    '}
                <Text color={reach ? 'green' : 'red'}>{reach ? '●' : '○'}</Text>
                {`  ${String(p['id'] ?? '?').padEnd(14)} ${String(p['kind'] ?? '?').padEnd(9)} ${String(p['model'] ?? '—')}${reason}`}
              </Text>,
            );
          });
        }
      } else if (name === 'work') {
        const pool = (poolRef.current ??= new Pool(maxAgents));
        const jobs = await dispatchPending(srv, codedir, hcfg, pool, claimedRef.current, hlog);
        if (!jobs.length) commit(<Text color={DIM}> (no new handovers)</Text>);
        else {
          commit(<Text color={DIM}>{`  → ${jobs.length} handover(s) started (≤${maxAgents} parallel), waiting …`}</Text>);
          const ok = (await Promise.all(jobs)).filter(Boolean).length;
          commit(<Text color={DIM}>{`  done: ${ok}/${jobs.length} cleanly uploaded`}</Text>);
        }
      } else if (name === 'auto') {
        const pool = (poolRef.current ??= new Pool(maxAgents));
        const arg = (payload.split(/\s+/)[1] ?? '').toLowerCase();
        if (arg === 'on') {
          if (autoRef.current) commit(<Text color={DIM}> [AUTO] already running</Text>);
          else {
            autoRef.current = setInterval(() => {
              void dispatchPending(srv, codedir, hcfg, pool, claimedRef.current, hlog);
            }, 5000);
            commit(<Text color={DIM}>{`  [AUTO] poller ON — pulls handovers every 5s, ≤${maxAgents} parallel`}</Text>);
          }
        } else if (arg === 'off') {
          if (autoRef.current) {
            clearInterval(autoRef.current);
            autoRef.current = null;
            commit(<Text color={DIM}> [AUTO] poller OFF</Text>);
          } else commit(<Text color={DIM}> [AUTO] was not active</Text>);
        } else {
          commit(<Text color={DIM}>{`  [AUTO] ${autoRef.current ? 'AN' : 'AUS'}  |  /auto on / /auto off`}</Text>);
        }
      }
    } catch (e) {
      // #454: HttpError carries the server's JSON error detail in its message — show that, not 'Error: …'.
      const msg = e instanceof Error ? e.message : String(e);
      commit(<Text color={ERROR}>{`  ✗ ${msg}`}</Text>);
    }
  }

  async function submit(line: string): Promise<void> {
    const {kind, name, payload} = classify(line);
    if (kind === 'empty') return;
    if (kind === 'local' && (name === 'exit' || name === 'quit')) {
      exit();
      return;
    }
    if (kind === 'local') {
      await handleLocal(name, payload);
      return;
    }
    await streamTurn(payload, kind === 'turn'); // MEM-11: server slash-commands aren't persisted
  }

  // #149: fetch the server's prompt/skill catalogue once, lazily, the first time a slash menu opens
  // (the session is open by then, so the gated GET passes). Fail-soft: any error → static commands only.
  useEffect(() => {
    if (!buffer.startsWith('/') || catalogueFetched.current) return;
    catalogueFetched.current = true; // latch now so concurrent slash-opens don't double-fetch
    void (async () => {
      try {
        setCatalogueCmds(catalogueToCommands(await srv.catalogue()));
      } catch {
        // no /catalogue (older server), gated/closed session, network blip, or a stub without
        // the method → static commands only; un-latch so a later slash-open can retry.
        catalogueFetched.current = false;
        setCatalogueCmds([]);
      }
    })();
  }, [buffer, srv]);

  // MEM-16(2): the slash-command suggestion list is open while the buffer is a slash prefix with
  // matches and the user hasn't dismissed it (Esc). completions() trims, so once an argument is
  // being typed the prefix stops matching and the menu closes on its own. #149: the server-fed
  // prompt items (catalogueCmds) are merged in so loaded prompts autocomplete as `/<name>`.
  const menuItems = useMemo(
    () => (buffer.startsWith('/') && !menuDismissed ? completions(buffer.slice(1), catalogueCmds) : []),
    [buffer, menuDismissed, catalogueCmds],
  );
  const menuOpen = !thinking && menuItems.length > 0;

  useInput((input, key) => {
    if (thinking) {
      // while a turn runs, Esc OR Ctrl+C cancels it (locally + on the server) instead of exiting
      if (key.escape || (key.ctrl && input === 'c')) cancelTurn();
      return;
    }
    // MEM-16(2): while the suggestion menu is open, Tab/↑/↓/Esc drive it; everything else falls through
    if (menuOpen) {
      const act = menuKey(menuSel, menuItems, key, buffer);
      if (act.type === 'move') {
        setMenuSel(act.sel);
        return;
      }
      if (act.type === 'complete') {
        setBuffer(completionText(act.cmd));
        setMenuSel(0);
        setMenuDismissed(true); // stays closed until the buffer is edited again
        return;
      }
      if (act.type === 'close') {
        setMenuDismissed(true);
        return;
      }
    }
    if (key.return) {
      const line = expandPastes(buffer, pasteStoreRef.current); // #438: sentinel tokens -> the raw paste
      setBuffer('');
      pasteStoreRef.current = newPasteStore();
      setMenuSel(0);
      setMenuDismissed(false);
      void submit(line);
      return;
    }
    if (key.ctrl && input === 'c') {
      exit();
      return;
    }
    if (key.backspace || key.delete) {
      setBuffer((b) => backspacePaste(b, pasteStoreRef.current)); // #438: one Backspace clears a whole [Pasted …] token + reclaims its block
      setMenuSel(0);
      setMenuDismissed(false);
      return;
    }
    if (key.tab) return; // swallow a stray Tab when the menu isn't open (no '\t' into the buffer)
    if (input && !key.ctrl && !key.meta) {
      // #438: a multi-line PASTE collapses to a [Pasted #N +L lines] placeholder (expanded on submit);
      // typed input and single-line pastes append verbatim. Strip the sentinel delimiters first so a
      // paste can never smuggle a forged token into the buffer.
      const safe = stripSentinels(input);
      if (key.paste && isMultilinePaste(safe)) {
        const token = storePaste(pasteStoreRef.current, safe);
        setBuffer((b) => b + token);
      } else {
        setBuffer((b) => b + safe);
      }
      setMenuSel(0);
      setMenuDismissed(false);
    }
  });

  return (
    <Box flexDirection="column" flexGrow={1}>
      <Box flexDirection="column" flexGrow={1}>
        <Static items={items}>{(item) => <Box key={item.id}>{item.node}</Box>}</Static>
      </Box>
      {liveAnswer ? (
        <Box paddingLeft={2} marginTop={1}>
          <Text>{liveAnswer}</Text>
        </Box>
      ) : null}
      {thinking ? <WorkingLine verb={verbRef.current} frame={frame} seconds={secs} tokens={tokens} /> : null}
      <InputBox buffer={buffer} caret={!thinking} />
      {menuOpen ? <CommandMenu items={menuItems} sel={menuSel} /> : null}
      <Footer st={status} />
      <Box flexDirection="row">
        <Spacer />
        {/* subtle bottom-right brand mark — single accent blue (no rainbow) */}
        <Text bold color={ACCENT}>
          Developed in the UAE
        </Text>
      </Box>
    </Box>
  );
}
