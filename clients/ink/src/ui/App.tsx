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
import {classify, completions, argCompletions, catalogueToCommands, HELP_TEXT, type Command} from '../commands.js';
import {chatStream, type NeedsGuide} from '../net/stream.js';
import {answerBody, createRouter} from '../stream/route.js';
import {renderMarkdown} from '../markdown.js';
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
import {splitToolBlocks} from './toolBlocks.js';
import {ToolCall} from './ToolCall.js';
import {runPassthroughTool} from '../tools/bridge.js';
import {runOperatorShell} from '../tools/runTool.js';
import {setDiagnosticSink} from '../tools/diagnostics.js';
import {runUpdate} from '../tools/update.js';
import {Pool, dispatchPending, type HandoverCfg, type HandoverLog} from '../agent/handover.js';
import {loadConfig, VERSION} from '../config.js';
import {load as loadSession, save as saveSession, clear as clearSession, transcriptStats, statePath} from '../state/persist.js';
import {ACCENT, DIM, ERROR, TEXT, VERBS} from './theme.js';

interface Item {
  id: number;
  node: ReactNode;
}

interface CommitOptions {
  tight?: boolean;
}

/** Render a committed turn body — markdown segments + foldable tool calls. Shared by the live commit AND the
 *  session restore (#1187), so restored history looks identical to fresh output (colours + folds), not the
 *  old dim plain text. */
export function renderTurnBody(body: string, wrap: number): React.ReactElement {
  const nodes: ReactNode[] = [];
  for (const s of splitToolBlocks(body)) {
    if (s.type === 'tool') {
      nodes.push(<ToolCall label={s.label} result={s.result} />);
    } else {
      const md = renderMarkdown(s.text, wrap);
      if (md) nodes.push(<Text>{md}</Text>);
    }
  }
  return (
    <Box flexDirection="column" paddingLeft={2}>
      {nodes.map((n, i) => (
        <Box key={i} marginTop={i === 0 ? 0 : 1}>
          {n}
        </Box>
      ))}
    </Box>
  );
}

/** One vertical-spacing rule for every block promoted into terminal scrollback (#1621). */
export function committedBlock(node: ReactNode): React.ReactElement {
  return <Box marginTop={1}>{node}</Box>;
}

/** A live continuation belongs to the preceding block and therefore adds no block-start margin (#1645). */
export function committedContinuation(node: ReactNode): React.ReactElement {
  return <Box>{node}</Box>;
}

export function renderStartupBanner(codedir: string, maxAgents: number): React.ReactElement {
  return (
    <Box flexDirection="column">
      <Text bold color={ACCENT}>
        █▀▄▀█ Ironclad <Text color={DIM}>· Orchestrator Client</Text>
      </Text>
      <Text color={DIM}>{`  Ironclad CLI ${VERSION} · code ${codedir} · ≤${maxAgents} agents`}</Text>
      <Text color={DIM}> /help · exit</Text>
    </Box>
  );
}

export function renderGuidedInput(g: NeedsGuide): React.ReactElement {
  return (
    <Box flexDirection="column">
      <Text color={DIM}>{`  guided input for /${g.command}:`}</Text>
      <Text color={DIM}>{`    usage: ${g.usage}`}</Text>
      {g.subcommands?.length ? <Text color={DIM}>{`    subcommands: ${g.subcommands.join(' | ')}`}</Text> : null}
      {(g.fields ?? []).map((f) => {
        const bits = [f.required ? 'required' : 'optional'];
        if (f.choices?.length) bits.push(`choices: ${f.choices.join('|')}`);
        if (f.default) bits.push(`default: ${f.default}`);
        return <Text key={f.name} color={DIM}>{`    ${f.name}  (${bits.join(', ')})`}</Text>;
      })}
    </Box>
  );
}

/** #454: HttpError carries the server's JSON error detail in its message — show that, not 'Error: …'. */
function operatorErrorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function App({
  srv,
  codedir,
  maxAgents,
  resume = false,
  operatorShell = runOperatorShell,
}: {
  srv: Server;
  codedir: string;
  maxAgents: number;
  resume?: boolean; // MEM-14: opt into resuming the persisted session (default = fresh)
  operatorShell?: typeof runOperatorShell;
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
      // INK-HANDOVER-1 (#503): pass the EXPLICIT client overrides (null if unset) so the server's
      // per-agent spec drives bin/template by default and a deliberate BYO override wins for Claude specs.
      claudeBinOverride: c.claudeBinExplicit,
      agentCmdOverride: c.agentCmdExplicit,
      claudeEffort: c.claudeEffort,
      claudePermissionMode: c.claudePermissionMode,
    };
  }, []);

  const commit = (node: ReactNode, options: CommitOptions = {}): void => {
    const id = idRef.current++;
    setItems((xs) => [...xs, {id, node: options.tight ? committedContinuation(node) : committedBlock(node)}]);
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
      commit(<Text color={DIM}>{`  ↻ restored ${turns} turn(s) (${lines} lines)`}</Text>);
      // #1187: render restored turns through the SAME path as fresh output (colours + folds), not dim plain.
      prev.transcript.forEach((tbody) => commit(renderTurnBody(tbody, Math.max(20, width - 4))));
      return true;
    }
    return false;
  };

  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;
    setDiagnosticSink((m) => commit(<Text color={DIM}>{`  ${m}`}</Text>));
    commit(renderStartupBanner(codedir, maxAgents));
    // MEM-14/MEM-18: resume is OPT-IN (default = fresh). With --resume we restore now; otherwise we
    // start clean and stay quiet — the saved session is kept on disk (persist keeps running) and the
    // goodbye on exit (cli.tsx) tells the user it can be brought back with /resume. No startup hint.
    if (resume && !resumeSession()) {
      commit(<Text color={DIM}> (no saved session to restore)</Text>);
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

  async function streamTurn(payload: string, conversational = true, echo?: string): Promise<void> {
    // #1278: echo what the operator TYPED — a slash-command keeps its leading `/` so it is visibly distinct
    // from a chat message (the wire `payload` is slash-stripped; only the echo shows the original input).
    const shown = echo ?? payload;
    commit(<Text color={DIM}>{`> ${shown}`}</Text>);
    // MEM-11: only real conversational turns are kept (persisted + summarised). Slash commands
    // (/status, /clear, /config, /ls, …) are repeatable — show them, but don't record them.
    if (conversational) transcriptRef.current.push(`> ${shown}`);
    const idx = Math.floor(Date.now() / 137) % VERBS.length;
    verbRef.current = VERBS[idx] ?? 'Working';
    t0Ref.current = Date.now();
    setSecs(0);
    setTokens(0);
    setThinking(true);
    setAgent(''); // #453: clear last turn's coder so a non-routed turn shows no stale "live" indicator
    setSearch(''); // S9: clear last turn's web-search summary
    setLiveAnswer('');
    let lastRender = 0;
    const router = createRouter();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const res = await chatStream(
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
              setLiveAnswer(answerBody(router));
            }
          },
          onTool: (f) => runPassthroughTool(srv, f, ac.signal),
          onRetry: (reason, _delayMs, nextAttempt, maxAttempts) =>
            commit(<Text color={DIM}>{`  ↻ ${reason} — retrying (${nextAttempt}/${maxAttempts})`}</Text>),
        },
        ac.signal,
      );
      if (res?.needs_confirm) {   // #935: destructive → not executed; re-run with --yes
        const ci = res.needs_confirm;
        // #956: the reason is the full localized line (reason + how-to-confirm) → print it single-language
        commit(<Text color={DIM}>{`  ⚠ ${ci.command}: ${ci.reason}`}</Text>);
        abortRef.current = null;
        setThinking(false); // #1304: the early return skips the tail — without this the client is
        return;             // wedged in thinking forever (input swallowed, Esc has nothing to abort)
      }
      if (res?.needs_guide) {   // #955: structured guided input — show the fields; nothing executed
        const g = res.needs_guide;
        commit(renderGuidedInput(g));
        abortRef.current = null;
        setThinking(false); // #1304: same leak as the needs_confirm branch
        return;
      }
    } catch (e) {
      // a user-initiated abort (Esc / Ctrl+C) is expected — note it quietly, don't show a ✗ error
      if (ac.signal.aborted) commit(<Text color={DIM}> cancelled</Text>);
      else {
        const msg = operatorErrorMessage(e);
        commit(<Text color={ERROR}>{`  ✗ ${msg}`}</Text>);
      }
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
      // #1167/#1187: markdown + foldable tool calls via the shared helper (also used on restore).
      commit(renderTurnBody(body, Math.max(20, width - 4)));
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
    const hlog: HandoverLog = (m, options): void => commit(<Text color={DIM}>{m}</Text>, options);
    try {
      if (name === 'help') commit(<Text color={TEXT}>{HELP_TEXT}</Text>);
      else if (name === 'sh') {
        // MEM-15: run a shell command locally (PowerShell on Windows) in the codedir — no server
        // turn, not persisted. Output to the transcript. (-NonInteractive: interactive cmds time out.)
        if (!payload) commit(<Text color={DIM}> (empty command)</Text>);
        else {
          commit(<Text color={DIM}>{`! ${payload}`}</Text>);
          const mark = idRef.current;
          const out = await operatorShell(payload);
          commit(<Box paddingLeft={2}><Text>{out}</Text></Box>, idRef.current === mark ? {tight: true} : undefined);
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
        commit(<Text color={DIM}> ↺ session reset (transcript + context + summary cleared; long-term memory kept)</Text>);
      } else if (name === 'resume') {
        // MEM-14: on-demand restore of the saved session (default start is fresh).
        if (!resumeSession()) commit(<Text color={DIM}> (no saved session)</Text>);
      } else if (name === 'update') {
        // MEM-17: rebuild + reinstall the global `ironclad` from source (GX10_SRC) — no manual
        // `cd clients/ink && …`. The Node process can't hot-swap itself, so it stages the build and
        // asks for a restart. `/update pull` does a git pull first.
        if (!srcDir) {
          commit(<Text color={ERROR}> /update needs the source path — set GX10_SRC (repo root) or srcDir in config.json.</Text>);
        } else {
          const pull = (payload.split(/\s+/)[1] ?? '').toLowerCase() === 'pull';
          commit(<Text color={DIM}>{`  /update — building + installing from ${srcDir}${pull ? ' (with git pull)' : ''} …`}</Text>);
          let mark = idRef.current;
          const {ok, log} = await runUpdate(srcDir, pull);
          log.forEach((l) => {
            commit(<Text color={ok ? DIM : ERROR}>{`  ${l}`}</Text>, idRef.current === mark ? {tight: true} : undefined);
            mark = idRef.current;
          });
        }
      } else if (name === 'health') commit(<Text color={DIM}>{`  ${JSON.stringify(await srv.health())}`}</Text>);
      else if (name === 'doctor') commit(<Text color={DIM}>{`  ${JSON.stringify(await srv.doctor())}`}</Text>); // DOCTOR (#503): local GET /doctor, not a billed turn
      else if (name === 'tasks') {
        const ts = await srv.tasks();
        if (!ts.length) commit(<Text color={DIM}> (no tasks)</Text>);
        else commit(
          <Box flexDirection="column">
            {ts.map((t) => <Text key={String(t['id'] ?? '?')}>{`  ${String(t['status'] ?? '?')}  ${String(t['id'] ?? '?')}  ${String(t['title'] ?? '')}`}</Text>)}
          </Box>,
        );
      } else if (name === 'pending') {
        const ps = await srv.pending();
        if (!ps.length) commit(<Text color={DIM}> (no open handovers)</Text>);
        else commit(
          <Box flexDirection="column">
            {ps.map((p) => <Text key={String(p['id'] ?? '?')}>{`  ${String(p['id'] ?? '?')}  ${String(p['title'] ?? '')}`}</Text>)}
          </Box>,
        );
      } else if (name === 'coders') {
        // #452: which coding agents are bound (● green) vs not found (○ red), then the fan-out lane.
        // #454: `/coders use <id>|auto` pins/clears the runtime coding agent.
        const parts = payload.split(/\s+/);
        const coderLines: ReactNode[] = [];
        if (parts.length >= 2 && parts[1]?.toLowerCase() === 'use') {
          const res = await srv.setCoderPin(parts[2] ?? 'auto');
          const pin = res['pinned'];
          coderLines.push(
            <Text color="cyan">
              {pin ? `  → pinned coder: ${String(pin)}` : '  → coder pin cleared (auto: the staged agent per task)'}
            </Text>,
          );
        }
        const d = await srv.coders();
        const coding = (d['coding_agents'] as Array<Record<string, unknown>>) ?? [];
        const pinned = d['pinned'] ? String(d['pinned']) : '';
        coderLines.push(
          <Text color={DIM}>
            {pinned ? `  pinned: ${pinned}  (/coders use auto to clear)` : '  routing: auto (orchestrator staged agent)'}
          </Text>,
        );
        if (!coding.length) coderLines.push(<Text color={DIM}> (no coding agents configured)</Text>);
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
          coderLines.push(
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
          coderLines.push(
            <Text color={DIM}>{`  providers (fan-out): ${prov['active'] ? 'active' : 'inactive'} · spent $${spent}`}</Text>,
          );
          pool.forEach((p) => {
            const reach = Boolean(p['reachable']);
            const reason = p['last_route_reason'] ? `  ← ${String(p['last_route_reason'])}` : '';
            coderLines.push(
              <Text>
                {'    '}
                <Text color={reach ? 'green' : 'red'}>{reach ? '●' : '○'}</Text>
                {`  ${String(p['id'] ?? '?').padEnd(14)} ${String(p['kind'] ?? '?').padEnd(9)} ${String(p['model'] ?? '—')}${reason}`}
              </Text>,
            );
          });
        }
        commit(
          <Box flexDirection="column">
            {coderLines.map((line, i) => <React.Fragment key={i}>{line}</React.Fragment>)}
          </Box>,
        );
      } else if (name === 'work') {
        const pool = (poolRef.current ??= new Pool(maxAgents));
        const earlyLogs: string[] = [];
        let headerCommitted = false;
        let mark = idRef.current;
        const workLog: HandoverLog = (m): void => {
          if (headerCommitted) {
            commit(<Text color={DIM}>{m}</Text>, idRef.current === mark ? {tight: true} : undefined);
            mark = idRef.current;
          }
          else earlyLogs.push(m);
        };
        const jobs = await dispatchPending(srv, codedir, hcfg, pool, claimedRef.current, workLog);
        if (!jobs.length) {
          const [first, ...rest] = earlyLogs;
          if (first !== undefined) commit(<Text color={DIM}>{first}</Text>);
          rest.forEach((line) => commit(<Text color={DIM}>{line}</Text>, {tight: true}));
          commit(<Text color={DIM}> (no new handovers)</Text>, first === undefined ? undefined : {tight: true});
        }
        else {
          commit(<Text color={DIM}>{`  → ${jobs.length} handover(s) started (≤${maxAgents} parallel), waiting …`}</Text>);
          headerCommitted = true;
          mark = idRef.current;
          earlyLogs.forEach((line) => {
            commit(<Text color={DIM}>{line}</Text>, idRef.current === mark ? {tight: true} : undefined);
            mark = idRef.current;
          });
          const ok = (await Promise.all(jobs)).filter(Boolean).length;
          commit(
            <Text color={DIM}>{`  done: ${ok}/${jobs.length} cleanly uploaded`}</Text>,
            idRef.current === mark ? {tight: true} : undefined,
          );
        }
      } else if (name === 'auto') {
        // #1296: /auto is the CONSOLIDATED automation switch. The client half is the dispatch
        // poller (pull /pending, run coders locally); the engine half (watcher + autopilot +
        // continuation) is mirrored by forwarding the same command — one verb drives both sides.
        const pool = (poolRef.current ??= new Pool(maxAgents));
        const arg = (payload.split(/\s+/)[1] ?? '').toLowerCase();
        if (arg === 'on') {
          if (autoRef.current) commit(<Text color={DIM}> [AUTO] poller already running</Text>);
          else {
            autoRef.current = setInterval(() => {
              void dispatchPending(srv, codedir, hcfg, pool, claimedRef.current, hlog);
            }, 5000);
            commit(<Text color={DIM}>{`  [AUTO] client poller ON — pulls handovers every 5s, ≤${maxAgents} local coders parallel`}</Text>);
          }
        } else if (arg === 'off') {
          if (autoRef.current) {
            clearInterval(autoRef.current);
            autoRef.current = null;
            commit(<Text color={DIM}> [AUTO] poller OFF</Text>);
          } else commit(<Text color={DIM}> [AUTO] poller was not active</Text>);
        } else {
          commit(<Text color={DIM}>{`  [AUTO] poller ${autoRef.current ? 'ON' : 'OFF'}  |  /auto on [N] / /auto off`}</Text>);
        }
        await streamTurn(payload, false, `/${payload}`);
      }
    } catch (e) {
      const msg = operatorErrorMessage(e);
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
    if (kind === 'suggest') {   // #934: unknown command → did-you-mean hint, never forwarded (no turn)
      commit(<Text color={DIM}>{`  unknown command — did you mean  /${name} ?`}</Text>);
      return;
    }
    await streamTurn(payload, kind === 'turn', line.trim()); // #1278: echo the original input (keeps the slash)
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
    () => {
      if (!buffer.startsWith('/') || menuDismissed) return [];
      // #937: once past the verb, offer argument completions (subcommands / flags / choices) from the
      // command-spec; while still typing the verb, plain name completion (with the prompt catalogue).
      const args = argCompletions(buffer, catalogueCmds);
      return args.length ? args : completions(buffer.slice(1), catalogueCmds);
    },
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
        setBuffer(completionText(act.cmd, buffer));
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
      {/* #1148: the SCROLLABLE region — transcript + the live streaming tail. mount scrolls this. */}
      <Box flexDirection="column" flexGrow={1}>
        <Static items={items}>{(item) => <Box key={item.id}>{item.node}</Box>}</Static>
        {/* #1277: fold tool calls in the LIVE preview via the SAME path as the commit, so a tool call is
            collapsed WHILE it streams instead of showing expanded and folding only at the end of the turn. */}
        {liveAnswer ? <Box marginTop={1}>{renderTurnBody(liveAnswer, Math.max(20, width - 4))}</Box> : null}
      </Box>
      {/* #1148: the FIXED chrome — the working/thinking line + input + menu + footer + brand. mount stamps
          this at the viewport bottom (paintFixed) so it stays pinned while the transcript above scrolls
          behind it. MUST be the LAST element child of this root Box (mount identifies it by that). The live
          STREAMING answer scrolls with the transcript (above); the WORKING line stays pinned near the input,
          like Claude Code. */}
      <Box flexDirection="column">
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
    </Box>
  );
}
