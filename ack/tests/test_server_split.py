"""Server/client split: the headless orchestrator server (engine/server.py).

Validates the pieces that make the split work WITHOUT touching the model:
  - the headless capture sink isolates output to the calling thread (a /chat request
    collects exactly its own turn's ``_ui_print`` output; other threads do not leak in)
  - ``_write_feedback`` drops the file the reconciler advances on
  - ``_pending_handovers`` surfaces a staged handover for the client to pull
  - the HTTP routes (/health, /chat, /tasks, /feedback) work end to end against a
    real ThreadingHTTPServer with a stubbed agent + dispatch (no vLLM)
"""
from __future__ import annotations

import json
import sys
import threading
import types
import urllib.request
from pathlib import Path

# Stub the heavy optional dep so the engine imports without openai installed.
sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

# core/engine on sys.path so `import gx10` / `import server` work (conftest adds core/).
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def _sink():
    """Install the capture sink for the test and restore afterwards."""
    prev = gx10._UI_SINK
    gx10._UI_SINK = server._capture_sink
    yield
    gx10._UI_SINK = prev


# --------------------------------------------------------------------------- #
# Capture sink — per-thread isolation.
# --------------------------------------------------------------------------- #
def test_capture_collects_this_threads_output():
    with server._Captured() as cap:
        gx10._ui_print("alpha")
        gx10._ui_print("beta")
    assert "alpha" in cap.text and "beta" in cap.text


def test_strip_chat_chrome_removes_status_markers_keeps_answer():
    # #921 (fachtest): the captured /chat output must not carry the [GX10]/[Qwen (planning)] pane chrome
    raw = "  [Qwen (planning)]\n\n[GX10]\n\n\n391\n\n  [perf] TTFT 1.6s\n\n  ==== DONE ====\n"
    out = server._strip_chat_chrome(raw)
    assert "391" in out and "[perf]" in out and "DONE" in out            # answer + status kept
    assert "[GX10]" not in out and "Qwen (planning)" not in out          # chrome markers dropped
    assert "\n\n\n" not in out                                           # blank runs collapsed
    # ANSI-wrapped + running variant are stripped too; plain text is untouched
    assert server._strip_chat_chrome("\x1b[36m[GX10]\x1b[0m\nhi") == "hi"
    assert "[Qwen (running)]" not in server._strip_chat_chrome("[Qwen (running)]\nanswer")
    assert server._strip_chat_chrome("just the answer") == "just the answer"


def test_capture_does_not_leak_across_threads():
    other_emitted = threading.Event()

    def _bg():
        # No buffer opened on this thread → must NOT land in the request buffer.
        gx10._ui_print("BACKGROUND_NOISE")
        other_emitted.set()

    with server._Captured() as cap:
        t = threading.Thread(target=_bg)
        t.start()
        assert other_emitted.wait(2.0)
        t.join()
        gx10._ui_print("request_line")
    assert "request_line" in cap.text
    assert "BACKGROUND_NOISE" not in cap.text


# --------------------------------------------------------------------------- #
# Feedback drop + pending discovery (file contract with the reconciler).
# --------------------------------------------------------------------------- #
def test_write_feedback_creates_reconciler_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")          # B3: feedback routes to the active initiative
    path = server._write_feedback("KGC-7", "opus", "## Result\nok")
    p = Path(path)
    assert p.name == "KGC-7_OPUS-feedback.md"
    assert p.parent.name == "feedback"
    assert p.parent.parent.name == ".work"         # inbox lives under <initiative>/.work/
    assert "vault" in p.parts and "demo" in p.parts # …and under vault/<slug>/, not the project root
    assert "## Result" in p.read_text(encoding="utf-8")


def test_pending_handovers_surfaces_staged_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")          # B3: routing target
    store = gx10._store()                           # the production singleton (routes to the initiative)
    store.create({"type": "feature", "priority": "high",
                  "title": "wire it", "description": "do the thing"}, force=True)
    tid = store.list("pending")[0]["id"]
    ho_dir = gx10.handovers_dir()                   # <initiative>/.work/handovers
    ho_dir.mkdir(parents=True, exist_ok=True)
    (ho_dir / f"{tid}_OPUS.md").write_text("---\nto: claude-opus-4-8\neffort: high\n---\nbody",
                                           encoding="utf-8")
    pend = server._pending_handovers()
    assert len(pend) == 1
    item = pend[0]
    assert item["id"] == tid
    assert item["agent"] == "OPUS"
    assert "body" in item["handover"]
    assert item["model"] == "claude-opus-4-8"
    assert item["effort"] == "high"


def test_pending_handover_agent_name_in_to_is_not_the_model(tmp_path, monkeypatch):
    # #1279 (completes #1236 on the /pending path — the guard existed only in _do_launch): the handover's
    # `to:` is the RECIPIENT AGENT ("to: OPUS"), which `_parse_handover_meta` returns as `model`. An agent id
    # must NOT be shipped as the model — before the fix /pending shipped `model: "OPUS"`, so the client
    # rendered `-m OPUS` and a non-Claude coder CLI failed (exit 1, e.g. CODEX "unknown option '-m'").
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")
    store = gx10._store()
    store.create({"type": "feature", "priority": "high", "title": "wire it", "description": "x"}, force=True)
    tid = store.list("pending")[0]["id"]
    ho_dir = gx10.handovers_dir()
    ho_dir.mkdir(parents=True, exist_ok=True)
    (ho_dir / f"{tid}_OPUS.md").write_text("---\nto: OPUS\neffort: high\n---\nbody", encoding="utf-8")
    item = server._pending_handovers()[0]
    assert item["agent"] == "OPUS"
    assert item["model"] == "claude-opus-4-8"        # spec.model — NOT the agent name "OPUS"
    assert item["effort"] == "high"                  # the orchestrator's effort choice is still honoured


def _stage_opus(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")
    store = gx10._store()
    store.create({"type": "feature", "priority": "high", "title": "wire it", "description": "x"}, force=True)
    tid = store.list("pending")[0]["id"]
    ho_dir = gx10.handovers_dir()
    ho_dir.mkdir(parents=True, exist_ok=True)
    (ho_dir / f"{tid}_OPUS.md").write_text("---\nto: OPUS\n---\nbody", encoding="utf-8")


def test_pending_handover_ships_the_resolved_bin_path(tmp_path, monkeypatch):
    # #1279: /pending must ship the boot-probe RESOLVED bin path (the exact executable /coders shows), NOT the
    # logical `spec.bin` — else the client spawns a bare `codex` and node's PATH picks the wrong install.
    _stage_opus(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_probe_cached", lambda: {"OPUS": r"C:\resolved\claude.EXE"})
    item = server._pending_handovers()[0]
    assert item["bin"] == r"C:\resolved\claude.EXE"   # the resolved path, not the logical bin


def test_pending_handover_bin_falls_back_to_spec_when_unresolved(tmp_path, monkeypatch):
    # #1279: an unresolved probe (agent absent on this machine) falls back to the logical spec.bin.
    _stage_opus(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_probe_cached", lambda: {})   # nothing resolved
    item = server._pending_handovers()[0]
    assert item["bin"] == "claude"                    # spec.bin fallback (OPUS's logical bin)


def test_pending_handover_ships_the_exec_cwd(tmp_path, monkeypatch):
    # #1307: /pending must ship the code root the client launches the coder IN (the active project's exec
    # cwd). Without it the client spawned the coder in its own stale startup `codedir` (the boot workdir)
    # and wrote one project's code into another project's tree — a project-isolation escape.
    _stage_opus(tmp_path, monkeypatch)
    item = server._pending_handovers()[0]
    assert item["cwd"]                                        # a concrete cwd is shipped
    assert isinstance(item["cwd"], str)                       # wire contract: JSON string, never a Path
    assert Path(item["cwd"]).resolve() == Path.cwd().resolve()  # == the active project's exec cwd


def test_pending_handover_cwd_honours_code_subdir(tmp_path, monkeypatch):
    # #1307/#1237: with a code_subdir configured the shipped cwd is <root>/<code_subdir> (created on
    # demand), so the client builds the product tree isolated from the control-plane (vault/, .ironclad/).
    _stage_opus(tmp_path, monkeypatch)
    monkeypatch.setattr(gx10, "CODE_SUBDIR", "src")
    item = server._pending_handovers()[0]
    assert Path(item["cwd"]).name == "src"
    assert Path(item["cwd"]).is_dir()                         # created on demand by _exec_cwd


# --------------------------------------------------------------------------- #
# HTTP routes end to end (real server, stubbed agent + dispatch).
# --------------------------------------------------------------------------- #
class _StubAgent:
    model = "stub-model"


class _StubWorkers:
    def fanout(self, prompts, *, system=None, max_tokens=None, temperature=0.7, think=True):
        return [{"ok": True, "content": f"r:{p}", "think": think} for p in prompts]


def _start_server(monkeypatch, tmp_path):
    from http.server import ThreadingHTTPServer

    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Server Demo", "software")    # B3: active initiative so /feedback can route

    def _fake_dispatch(agent, message):
        gx10._ui_print(f"echo:{message}")

    monkeypatch.setattr(gx10, "_dispatch", _fake_dispatch)

    server._Handler.agent = _StubAgent()
    server._Handler.cfg = {"connection": {"base_url": "http://localhost:8000/v1"}}
    server._Handler.workers = _StubWorkers()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def test_http_health_and_chat_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", False, raising=False)
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        health = _get(port, "/health")
        assert health["ok"] and health["model"] == "stub-model"
        assert health["watcher"] is False
        # #385: Cold (memory) and Warm tiers are reported SEPARATELY; with neither configured in the stub
        # both read "off" (a Warm outage can no longer hide behind a Cold-only `memory: up`).
        assert health["memory"] == "off" and health["warm"] == "off"
        # #601 isolation observability: /health surfaces the project-registry binding (status/active/home).
        assert isinstance(health.get("registry"), dict) and "status" in health["registry"]

        res = _post(port, "/chat", {"message": "ping"})
        assert res["ok"] and "echo:ping" in res["output"]

        assert _get(port, "/tasks") == {"tasks": []}

        fb = _post(port, "/feedback", {"task_id": "KGC-1", "agent": "OPUS", "content": "done"})
        assert fb["ok"]
        assert Path(fb["feedback_file"]).read_text(encoding="utf-8") == "done"

        fo = _post(port, "/fanout", {"prompts": ["x", "y"], "think": False})
        assert fo["ok"]
        assert [r["content"] for r in fo["results"]] == ["r:x", "r:y"]
        assert fo["results"][0]["think"] is False
    finally:
        httpd.shutdown()


def test_health_reports_warm_tier_up_down_off(tmp_path, monkeypatch):
    # #385: /health distinguishes the Warm (Valkey) tier — up (reachable PING), down (configured but
    # unreachable → the silent-no-op case), off (not configured). Read at request time from gx10._WARM.
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        class _Warm:
            def __init__(self, ok): self._ok = ok
            def is_available(self): return self._ok
        monkeypatch.setattr(gx10, "_WARM", _Warm(True), raising=False)
        assert _get(port, "/health")["warm"] == "up"
        monkeypatch.setattr(gx10, "_WARM", _Warm(False), raising=False)
        assert _get(port, "/health")["warm"] == "down"       # outage no longer hides behind Cold's memory:up
        monkeypatch.setattr(gx10, "_WARM", None, raising=False)
        assert _get(port, "/health")["warm"] == "off"
    finally:
        httpd.shutdown()


def test_http_fanout_requires_prompts(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        try:
            _post(port, "/fanout", {"prompts": []})
            assert False, "expected HTTP 400"
        except urllib.error.HTTPError as e:  # type: ignore[name-defined]
            assert e.code == 400
    finally:
        httpd.shutdown()


def test_http_feedback_rejects_unknown_agent(tmp_path, monkeypatch):
    # #449 (review B-6): /feedback is fail-closed — an unknown/missing agent is rejected (400), not
    # silently defaulted to OPUS (which would drop a feedback file that never advances the task).
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        for bad in ({"task_id": "KGC-1", "agent": "BOGUS", "content": "x"},
                    {"task_id": "KGC-1", "content": "x"}):          # missing agent
            try:
                _post(port, "/feedback", bad)
                assert False, "expected HTTP 400"
            except urllib.error.HTTPError as e:  # type: ignore[name-defined]
                assert e.code == 400
        # a configured agent still works
        ok = _post(port, "/feedback", {"task_id": "KGC-1", "agent": "sonnet", "content": "done"})
        assert ok["ok"] and ok["feedback_file"].endswith("KGC-1_SONNET-feedback.md")
    finally:
        httpd.shutdown()


def test_http_chat_stream(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/chat/stream",
            data=json.dumps({"message": "ping"}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8")  # liest bis EOF (Connection: close)
        assert "echo:ping" in body
    finally:
        httpd.shutdown()


def test_http_cancel_sets_event(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        gx10._CANCEL_EVENT.clear()
        res = _post(port, "/cancel", {})
        assert res["ok"] and res["cancelled"] is True
        assert gx10._CANCEL_EVENT.is_set()      # running turn would see this and abort
    finally:
        gx10._CANCEL_EVENT.clear()
        httpd.shutdown()


def test_http_chat_requires_message(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        try:
            _post(port, "/chat", {"message": "   "})
            assert False, "expected HTTP 400"
        except urllib.error.HTTPError as e:  # type: ignore[name-defined]
            assert e.code == 400
    finally:
        httpd.shutdown()


import urllib.error  # noqa: E402  (used in the 400 assertion above)


# ── #452: GET /coders + /health coders block ────────────────────────────────────────────────────
def test_coders_snapshot_degrades_without_dispatcher(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", None, raising=False)
    snap = server._coders_snapshot()
    ids = {a["id"] for a in snap["coding_agents"]}
    assert {"OPUS", "SONNET"} <= ids                      # default registry coding agents
    assert all("bound" in a and "model" in a for a in snap["coding_agents"])
    assert snap["providers"]["active"] is False           # no dispatcher → degraded, never raises


def test_coders_snapshot_shows_onboarded_disabled_agent(monkeypatch):
    # #460: an onboarded-but-disabled agent (enabled:false, e.g. KIMI pending calibration) is INERT but
    # appears in /coders as registered (enabled:false, bound:false) so the operator can see it.
    cfg = gx10._code_defaults()
    cfg["code_agents"]["pool"].append({
        "provider_id": "kimi", "kind": "cli", "agent_id": "KIMI", "model": "kimi",
        "bin": "kimi", "cmd_template": "{bin} -p {prompt}", "enabled": False})
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg, raising=False)
    monkeypatch.setattr(gx10, "_DISPATCHER", None, raising=False)
    snap = server._coders_snapshot()
    by = {a["id"]: a for a in snap["coding_agents"]}
    assert by["OPUS"]["enabled"] is True
    assert "KIMI" in by and by["KIMI"]["enabled"] is False and by["KIMI"]["bound"] is False  # onboarded, inert


def test_coders_health_counts(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", None, raising=False)
    h = server._coders_health()
    assert h["total"] >= 2 and 0 <= h["bound"] <= h["total"]


def test_http_coders_and_health_block(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        d = _get(port, "/coders")
        assert "coding_agents" in d and "providers" in d
        assert {"OPUS", "SONNET"} <= {a["id"] for a in d["coding_agents"]}
        h = _get(port, "/health")
        assert "coders" in h and h["coders"]["total"] >= 2
    finally:
        httpd.shutdown()


def test_probe_cache_reuses_within_ttl(monkeypatch):
    # #452 (review A perf): /health re-derives the coders count every 2s — the boot probe is cached
    # for a short TTL so the poll reuses it instead of stat-ing the filesystem each time.
    import providers
    monkeypatch.setattr(server, "_PROBE_CACHE", {"at": -1e9, "data": None}, raising=False)
    calls = {"n": 0}
    real = providers.probe_code_agents
    monkeypatch.setattr(providers, "probe_code_agents",
                        lambda reg: (calls.__setitem__("n", calls["n"] + 1), real(reg))[1])
    server._probe_cached()
    server._probe_cached()
    assert calls["n"] == 1                                # second call served from cache (within TTL)
    # an expired entry re-probes
    server._PROBE_CACHE["at"] = -1e9
    server._probe_cached()
    assert calls["n"] == 2


# ── #453: [agent] control frames (routing provenance → client footer) ────────────────────────────
def test_emit_agent_frames(monkeypatch):
    captured: list = []
    monkeypatch.setattr(gx10, "_ui_print", lambda s, *a, **k: captured.append(s))
    gx10._emit_agent_frames([
        {"provider_id": "codex", "route_reason": "cheapest-capable", "ok": True},
        {"provider_id": "codex", "route_reason": "cheapest-capable", "ok": True},   # dup → ONE frame
        {"provider_id": "spark-vllm", "route_reason": "local-idle", "spilled": True, "ok": True},
        {"ok": True},                                    # no provider_id (byte-identical fanout) → skipped
    ])
    text = "\n".join(captured)
    assert text.count("[agent]") == 2                    # one frame per DISTINCT routed provider
    assert "[agent] codex · cheapest-capable" in text
    assert "[agent] spark-vllm · local-idle · spilled" in text


def test_emit_agent_frames_failsoft(monkeypatch):
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    gx10._emit_agent_frames("not a list")                # never raises on a bad shape
    gx10._emit_agent_frames([None, 42, {"route_reason": "x"}])   # malformed/no-id entries skipped


def test_app_status_text_shows_coder_live():
    # #453: the Textual client surfaces the live coder — in the spinner WHILE thinking (the spinner
    # replaces the footer during a turn) and in the footer when idle.
    import app as appmod
    inst = appmod.IroncladApp.__new__(appmod.IroncladApp)
    inst._status = {"model": "m", "connected": True, "watcher": False, "autopilot": False,
                    "pending": 0, "in_progress": 0, "done": 0, "perf": "",
                    "agent": "codex · cheapest-capable"}
    inst._spin = 0
    inst._think_t0 = 0.0
    inst._thinking = True
    assert "coder codex" in str(inst._status_text())     # live during the turn (spinner line)
    inst._thinking = False
    assert "coder codex" in str(inst._status_text())     # footer when idle
    inst._status["agent"] = ""                           # #453 (review B): turn-start clear → no stale coder
    inst._thinking = True
    assert "coder" not in str(inst._status_text())
    inst._thinking = False
    assert "coder" not in str(inst._status_text())


# ── #454: runtime agent switching (operator pin overrides the staged agent) ──────────────────────
def test_effective_code_agent_pin_overrides(monkeypatch):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    assert gx10._effective_code_agent("OPUS") == "OPUS"        # no pin → staged
    gx10._EFFECTIVE_CFG["code_agents"]["pinned"] = "SONNET"
    assert gx10._effective_code_agent("OPUS") == "SONNET"      # pin overrides the staged agent
    gx10._EFFECTIVE_CFG["code_agents"]["pinned"] = "BOGUS"     # unknown pin → fail-closed (staged)
    assert gx10._effective_code_agent("OPUS") == "OPUS"
    gx10._EFFECTIVE_CFG["code_agents"]["pinned"] = None
    assert gx10._effective_code_agent("OPUS") == "OPUS"


def test_set_coder_pin_validate_set_clear(monkeypatch):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    assert server._set_coder_pin("sonnet") == {"pinned": "SONNET"}
    assert gx10._code_agent_pin() == "SONNET"
    for clear in ("auto", "", None, "off"):
        assert server._set_coder_pin(clear) == {"pinned": None}
    import pytest as _pt
    with _pt.raises(ValueError, match="unknown agent"):
        server._set_coder_pin("bogus")


def test_pending_handover_honors_pin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("PinDemo", "software")
    store = gx10._store()
    store.create({"type": "feature", "priority": "high", "title": "wire", "description": "x"}, force=True)
    tid = store.list("pending")[0]["id"]
    ho_dir = gx10.handovers_dir()
    ho_dir.mkdir(parents=True, exist_ok=True)
    # staged OPUS WITH frontmatter (the orchestrator's model choice for OPUS)
    (ho_dir / f"{tid}_OPUS.md").write_text("---\nto: claude-opus-4-8\neffort: xhigh\n---\nbody",
                                           encoding="utf-8")
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    gx10._EFFECTIVE_CFG["code_agents"]["pinned"] = "SONNET"            # operator pins SONNET
    item = server._pending_handovers()[0]
    assert item["agent"] == "SONNET"                                  # the pin overrode the staged OPUS
    # #454 (review B): the pinned agent runs ITS OWN model — NOT the staged handover's `to:` frontmatter
    assert item["model"] == "claude-sonnet-5"
    assert item["effort"] == "high"                                  # SONNET's spec effort, not xhigh


def test_http_coders_pin_set_clear_reject(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
        r = _post(port, "/coders", {"agent": "sonnet"})
        assert r["ok"] and r["pinned"] == "SONNET"
        assert _get(port, "/coders")["pinned"] == "SONNET"            # GET reflects the pin
        r = _post(port, "/coders", {"agent": "auto"})
        assert r["ok"] and r["pinned"] is None
        try:
            _post(port, "/coders", {"agent": "bogus"})
            assert False, "expected HTTP 400"
        except urllib.error.HTTPError as e:  # type: ignore[name-defined]
            assert e.code == 400
    finally:
        httpd.shutdown()


def test_reconciler_matches_pinned_agent_feedback(tmp_path, monkeypatch):
    # #454: with a pin, the executing (effective) agent writes {tid}_{pin}-feedback.md — the reconciler
    # must match it to the staged task (look for the EFFECTIVE agent's feedback, not only the staged one).
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("PinRec", "software")
    store = gx10._store()
    store.create({"type": "feature", "priority": "high", "title": "t", "description": "d",
                  "assigned_to": "OPUS"}, force=True)
    tid = store.list("pending")[0]["id"]
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    gx10._EFFECTIVE_CFG["code_agents"]["pinned"] = "SONNET"
    fb_dir = gx10.feedback_dir()
    fb_dir.mkdir(parents=True, exist_ok=True)
    (fb_dir / f"{tid}_SONNET-feedback.md").write_text("done", encoding="utf-8")   # effective-agent feedback
    captured: list = []
    seen: dict = {}
    enq: set = set()
    gx10._reconcile_once(store, lambda *a: captured.append(a), seen, enq)   # tick 1: records mtime
    gx10._reconcile_once(store, lambda *a: captured.append(a), seen, enq)   # tick 2: mtime stable → enqueue
    assert any(c[0] == tid and c[1] == "SONNET" for c in captured)   # advanced under the effective agent


def test_reconciler_pin_change_falls_back_to_staged_feedback(tmp_path, monkeypatch):
    # #454 (review A): the pin changed mid-handover (effective=SONNET) but the work completed under the
    # STAGED agent (OPUS) — the reconciler falls back to {tid}_OPUS-feedback.md so the task still advances.
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("PinFb", "software")
    store = gx10._store()
    store.create({"type": "feature", "priority": "high", "title": "t", "description": "d",
                  "assigned_to": "OPUS"}, force=True)
    tid = store.list("pending")[0]["id"]
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    gx10._EFFECTIVE_CFG["code_agents"]["pinned"] = "SONNET"            # effective = SONNET
    fb_dir = gx10.feedback_dir()
    fb_dir.mkdir(parents=True, exist_ok=True)
    (fb_dir / f"{tid}_OPUS-feedback.md").write_text("done", encoding="utf-8")   # ONLY the staged feedback
    captured: list = []
    seen: dict = {}
    enq: set = set()
    gx10._reconcile_once(store, lambda *a: captured.append(a), seen, enq)
    gx10._reconcile_once(store, lambda *a: captured.append(a), seen, enq)
    assert any(c[0] == tid and c[1] == "OPUS" for c in captured)      # fell back to the staged agent


def test_reconciler_advances_after_pin_cleared_post_run(tmp_path, monkeypatch):
    # #454 (review B round 4): the pin was active during execution (SONNET ran, wrote {tid}_SONNET-
    # feedback.md) then was CLEARED before reconcile — the reconciler must still DISCOVER + advance the
    # SONNET feedback (not only look at the now-effective staged OPUS), or the task strands.
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("PinClr", "software")
    store = gx10._store()
    store.create({"type": "feature", "priority": "high", "title": "t", "description": "d",
                  "assigned_to": "OPUS"}, force=True)
    tid = store.list("pending")[0]["id"]
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    gx10._EFFECTIVE_CFG["code_agents"]["pinned"] = None              # pin CLEARED at reconcile time
    fb_dir = gx10.feedback_dir()
    fb_dir.mkdir(parents=True, exist_ok=True)
    (fb_dir / f"{tid}_SONNET-feedback.md").write_text("done", encoding="utf-8")   # SONNET ran (pinned)
    captured: list = []
    seen: dict = {}
    enq: set = set()
    gx10._reconcile_once(store, lambda *a: captured.append(a), seen, enq)
    gx10._reconcile_once(store, lambda *a: captured.append(a), seen, enq)
    assert any(c[0] == tid and c[1] == "SONNET" for c in captured)   # discovered the pinned-run feedback


# ── #455: circuit-breaker + equal-peer failover + /feedback classification ───────────────────────
@pytest.fixture
def _clean_breaker():
    gx10._CODE_AGENT_BREAKER.clear()
    yield
    gx10._CODE_AGENT_BREAKER.clear()


def test_breaker_trip_reset_snapshot(_clean_breaker):
    assert not gx10._breaker_tripped("CODEX")
    gx10._breaker_trip("codex", "budget")
    assert gx10._breaker_tripped("CODEX") and gx10._breaker_snapshot() == {"CODEX": "budget"}
    gx10._breaker_reset("CODEX")
    assert not gx10._breaker_tripped("CODEX")


def test_effective_agent_fails_over_when_tripped(_clean_breaker, monkeypatch):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)  # OPUS+SONNET
    assert gx10._effective_code_agent("OPUS") == "OPUS"        # not tripped → chosen
    gx10._breaker_trip("OPUS")
    assert gx10._effective_code_agent("OPUS") == "SONNET"      # OPUS tripped → cheapest non-tripped peer
    gx10._breaker_trip("SONNET")
    assert gx10._effective_code_agent("OPUS") == "OPUS"        # ALL tripped → keep chosen (fail-closed)


def test_http_feedback_classifies_exhausted_trips_breaker(tmp_path, monkeypatch, _clean_breaker):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
        # a no-feedback run whose stderr says "rate limit" → agent-unavailable → breaker trips
        r = _post(port, "/feedback", {"task_id": "KGC-1", "agent": "OPUS", "content": "",
                                      "exit_code": 1, "stderr": "Error: rate limit exceeded"})
        assert r["ok"] and r["classification"] == "agent-unavailable"
        assert gx10._breaker_tripped("OPUS")
        # a no-feedback run with no exhausted signal → task-failed (NOT a failover), no breaker trip
        r2 = _post(port, "/feedback", {"task_id": "KGC-2", "agent": "SONNET", "content": "",
                                       "exit_code": 1, "stderr": "compile error"})
        assert r2["classification"] == "task-failed" and not gx10._breaker_tripped("SONNET")
        # a real result → ok-feedback, feedback file written
        r3 = _post(port, "/feedback", {"task_id": "KGC-3", "agent": "SONNET", "content": "done"})
        assert r3["classification"] == "ok-feedback" and r3["feedback_file"].endswith("KGC-3_SONNET-feedback.md")
        # #455 (review B): feedback content that legitimately mentions a budget term must NOT false-trip
        gx10._breaker_reset("SONNET")
        r4 = _post(port, "/feedback", {"task_id": "KGC-4", "agent": "SONNET",
                                       "content": "Implemented rate limit + quota handling.", "exit_code": 0})
        assert r4["classification"] == "ok-feedback" and not gx10._breaker_tripped("SONNET")
    finally:
        httpd.shutdown()


def test_http_feedback_failed_no_feedback_marks_task_blocked(tmp_path, monkeypatch, _clean_breaker):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        gx10._store().create({"type": "bugfix", "priority": "high", "title": "t", "description": "d"}, force=True)
        tid = gx10._store().list("pending")[0]["id"]
        gx10._store().transition(tid, "in_progress")
        r = _post(port, "/feedback", {"task_id": tid, "agent": "OPUS", "content": "",
                                      "exit_code": 2, "stderr": "unknown model"})
        assert r["classification"] == "task-failed"
        t = gx10._store().get(tid)
        assert t["blocked_kind"] == "errored"
        assert "unknown model" in t["blocked_reason"]
    finally:
        httpd.shutdown()


def test_http_feedback_ok_does_not_mark_task_blocked(tmp_path, monkeypatch, _clean_breaker):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        gx10._store().create({"type": "bugfix", "priority": "high", "title": "t", "description": "d"}, force=True)
        tid = gx10._store().list("pending")[0]["id"]
        gx10._store().transition(tid, "in_progress")
        r = _post(port, "/feedback", {"task_id": tid, "agent": "OPUS", "content": "status: done\nok"})
        assert r["classification"] == "ok-feedback"
        assert not gx10._store().get(tid).get("blocked")
    finally:
        httpd.shutdown()


def test_coders_snapshot_shows_breaker_and_pin_resets(monkeypatch, _clean_breaker):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    gx10._breaker_trip("OPUS", "budget/quota exhausted")
    snap = server._coders_snapshot()
    opus = [a for a in snap["coding_agents"] if a["id"] == "OPUS"][0]
    assert opus["unavailable"] is True and opus["unavailable_reason"] == "budget/quota exhausted"
    server._set_coder_pin("opus")                            # pinning an agent clears its breaker
    assert not gx10._breaker_tripped("OPUS")


# ── #456: task_class derivation + task_class-scoped failover (capability matrix) ──────────────────
@pytest.mark.parametrize("ttype,expected", [
    ("security", "complex"), ("security-audit", "complex"), ("architecture", "complex"),
    ("optimization", "complex"), ("documentation", "routine"), ("cleanup", "routine"),
    ("verification", "analysis"), ("research", "analysis"),
    ("feature", "standard"), ("bugfix", "standard"), ("implementation", "standard"),
    ("", "standard"), ("unknown-type", "standard"),
])
def test_task_class_derivation(ttype, expected):
    assert gx10._task_class({"type": ttype}) == expected   # #1287: deterministic cost tier from task type


def test_class_capable_agents_failopen(monkeypatch):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    assert gx10._class_capable_agents("complex") == ["OPUS"]
    assert gx10._class_capable_agents("standard") == ["SONNET", "OPUS"]
    assert gx10._class_capable_agents("zzz") is None        # unknown class → no restriction (fail-open)
    assert gx10._class_capable_agents(None) is None


def test_failover_scoped_by_task_class(_clean_breaker, monkeypatch):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    gx10._breaker_trip("OPUS")
    # standard: SONNET is capable → failover to SONNET
    assert gx10._effective_code_agent("OPUS", task_class="standard") == "SONNET"
    # complex: only OPUS is capable → keep OPUS (NEVER fail over to a cheaper non-complex agent), fail-closed
    assert gx10._effective_code_agent("OPUS", task_class="complex") == "OPUS"
    # unknown class / no class → no restriction (byte-identical to #455)
    assert gx10._effective_code_agent("OPUS", task_class="zzz") == "SONNET"
    assert gx10._effective_code_agent("OPUS") == "SONNET"


def test_pending_handover_failover_stays_in_task_class(tmp_path, monkeypatch, _clean_breaker):
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("ClassDemo", "software")
    store = gx10._store()
    store.create({"type": "security", "priority": "high", "title": "audit", "description": "x"}, force=True)
    tid = store.list("pending")[0]["id"]
    ho_dir = gx10.handovers_dir()
    ho_dir.mkdir(parents=True, exist_ok=True)
    (ho_dir / f"{tid}_OPUS.md").write_text("body", encoding="utf-8")   # staged OPUS for a security task
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    gx10._breaker_trip("OPUS")                                        # OPUS budget-exhausted
    item = server._pending_handovers()[0]
    # only OPUS is security-capable → the failover keeps OPUS (it does NOT route a security task to SONNET)
    assert item["agent"] == "OPUS"


def test_class_capable_empty_list_fails_closed(_clean_breaker, monkeypatch):
    # #456 (review B / Codex S2): an EXPLICIT empty capability list means "no agent may serve this class"
    # → it must scope the failover to nothing (keep the chosen agent, fail-CLOSED), NOT collapse to fail-open.
    cfg = gx10._code_defaults()
    cfg["code_agents"]["classes"]["security"] = []          # operator: nothing is security-capable
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg, raising=False)
    assert gx10._class_capable_agents("security") == []     # mapped-but-empty → restrict to nothing
    assert gx10._class_capable_agents("zzz") is None         # still unmapped → fail-open
    gx10._breaker_trip("OPUS")
    # OPUS tripped, security capable set empty → NO peer → keep OPUS (never leaks to SONNET/CODEX)
    assert gx10._effective_code_agent("OPUS", task_class="security") == "OPUS"


def test_autopilot_launch_path_failover_is_task_class_scoped(tmp_path, monkeypatch, _clean_breaker):
    # #456 (review B / Codex S2): the autopilot reconciler LAUNCH path is a 2nd execution surface — it must
    # also scope the budget failover by task_class, else a tripped Opus on a security task leaks to a peer.
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("LaunchClass", "software")
    store = gx10._store()
    store.create({"type": "security", "priority": "high", "title": "audit", "description": "x"}, force=True)
    tid = store.list("pending")[0]["id"]
    ho_dir = gx10.handovers_dir()
    ho_dir.mkdir(parents=True, exist_ok=True)
    (ho_dir / f"{tid}_OPUS.md").write_text("body", encoding="utf-8")    # staged OPUS for a security task
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    monkeypatch.setattr(gx10, "AUTOPILOT_ENABLED", True, raising=False)
    monkeypatch.setattr(gx10, "AUTOPILOT_MAX_CONCURRENT", 0, raising=False)
    gx10._breaker_trip("OPUS")
    launched_cmds: list = []
    gx10._reconcile_once(store, lambda *a: None, {}, set(),
                         launch_enqueue=lambda tid, agent: launched_cmds.append((tid, agent)),
                         launched=set())
    # only OPUS is security-capable → the launch path keeps OPUS (does NOT launch a non-security agent)
    assert launched_cmds == [(tid, "OPUS")]


# ── #480/#994-S10: always-on read-only Memory MCP injection when memory + template exist ──────────
def _mcp_spec():
    import providers
    return providers.ProviderSpec(
        provider_id="claude-opus", kind="cli", agent_id="OPUS", model="claude-opus-4-8", bin="claude",
        cmd_template="{bin} {mcp} --print {prompt}",
        mcp_template='--mcp-config \'{"x":"{mcp_cmd}","y":"{mcp_script}"}\'')


def test_mcp_for_launch_is_always_on_when_memory_configured(monkeypatch):
    # #994-S10: the read-only Memory MCP is ALWAYS ON when memory + a per-CLI template — the profile no
    # longer gates it (a coder can only READ memory, never write).
    spec = _mcp_spec()
    monkeypatch.setattr(gx10, "_MEMORY_CONFIG", {"base_url": "http://mem:8800", "agent_id": "ironclad"}, raising=False)
    monkeypatch.delenv("GX10_PROFILE", raising=False)
    for profile in ("open", "sealed"):     # open (default) AND sealed both wire the memory MCP now
        monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"security": {"profile": profile}}, raising=False)
        args, env = gx10._mcp_for_launch(spec)
        assert "--mcp-config" in args and env["GX10_MEMORY_URL"] == "http://mem:8800"
        assert env["GX10_MCP_MEMORY_NS"] == "ironclad"            # project namespace, not the code-agent id


def test_mcp_for_launch_off_without_memory(monkeypatch):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"security": {"profile": "sealed"}}, raising=False)
    monkeypatch.setattr(gx10, "_MEMORY_CONFIG", {}, raising=False)   # sealed but no memory service → off
    assert gx10._mcp_for_launch(_mcp_spec()) == ("", {})


def test_pending_handover_carries_mcp_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("McpDemo", "software")
    store = gx10._store()
    store.create({"type": "feature", "priority": "high", "title": "t", "description": "d"}, force=True)
    tid = store.list("pending")[0]["id"]
    ho = gx10.handovers_dir(); ho.mkdir(parents=True, exist_ok=True)
    (ho / f"{tid}_OPUS.md").write_text("body", encoding="utf-8")
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)  # open profile default
    item = server._pending_handovers()[0]
    assert "mcp" in item and item["mcp"] == "" and item["mcp_env"] == {}   # present + empty under open


# ── #935: server-side confirm-before-execute gate (destructive only) ─────────────────────────────────
def test_confirm_required_gates_only_destructive_project_delete():
    assert server._confirm_required("/project delete demo")["command"] == "project delete"
    assert server._confirm_required("/project delete demo --purge")["tier"] == "destructive"
    assert server._confirm_required("/pj delete demo")["command"] == "project delete"   # alias resolves first
    assert server._confirm_required("/project list") is None       # a safe project op → no confirm
    assert server._confirm_required("/project new x") is None       # non-destructive project sub
    assert server._confirm_required("/config set x on") is None     # mutating, not destructive
    assert server._confirm_required("/autoplan on") is None         # costly, not destructive (operator scope)
    assert server._confirm_required("hello there") is None          # not a command → no gate


# ── #954: server-side structured guided-input contract (explicit ?/--guide only) ─────────────────────
def test_guide_required_explicit_trigger_returns_structured_fields():
    g = server._guide_required("/config set ?")
    assert g and g["command"] == "config set" and g["canonical_echo"] == "/config set" and "usage" in g
    names = {f["name"] for f in g["fields"]}
    assert "<dotted.key>" in names and any(f["required"] for f in g["fields"])
    lg = server._guide_required("/lifecycle gate --guide")   # --guide flag → structured guidance
    assert lg and lg["command"] == "lifecycle" and "gate" in lg["subcommands"]
    pj = server._guide_required("/project ?")                 # family verb → subcommands + rich usage
    assert pj and "delete" in pj["subcommands"] and "new <name>" in pj["usage"]
    assert any(f["choices"] for f in server._guide_required("/generate ?")["fields"])  # enum flag → choices


def test_guide_required_no_trigger_is_none():
    assert server._guide_required("/config set mpr.enabled on") is None   # partial command → dispatch, not guide
    assert server._guide_required("/status") is None                       # bare, no explicit trigger
    assert server._guide_required("hello there") is None                   # not a command
    assert server._guide_required("/nonesuch ?") is None                   # unknown verb


def test_render_guide_emits_fields_choices_and_defaults():
    # #955: the shared Python-client renderer for the needs_guide contract (client chrome is English)
    import client
    lines: list = []
    g = {"command": "config set", "subcommands": ["a", "b"], "usage": "usage: /config set <k> <v>",
         "fields": [{"name": "<dotted.key>", "required": True, "choices": [], "default": "", "type": "value"},
                    {"name": "--type", "required": False, "choices": ["mpr", "software"], "default": "sw", "type": "enum"}],
         "canonical_echo": "/config set"}
    client.render_guide(g, lines.append)
    blob = "\n".join(lines)
    assert "guided input for /config set" in blob and "usage: /config set <k> <v>" in blob
    assert "subcommands: a | b" in blob
    assert "<dotted.key>  (required)" in blob
    assert "choices: mpr|software" in blob and "default: sw" in blob


def test_confirm_message_is_single_language_full_line():
    # #956: the confirm reason is now the whole user-facing line (reason + how-to-confirm), so a client
    # prints it verbatim with no English wrapper mixing into a localized reason.
    r = server._confirm_required("/project delete demo")["reason"]
    assert "--yes" in r and "nothing changed" in r.lower()


# ── #962 regression: the confirm + guide gates must fire on the CLIENT WIRE FORM (slash-stripped) ────
def test_confirm_and_guide_fire_on_the_client_wire_form():
    # The bug: clients POST the slash-STRIPPED body (what _dispatch consumes), but the gates REQUIRED a
    # leading '/', so both contracts were dead through every client. This ties classify() (the real wire
    # payload) to the gate so it cannot silently regress.
    import commands   # sibling engine module (server import put engine/ on sys.path)
    _, _, payload = commands.classify("/project delete demo")
    assert payload == "project delete demo"                              # slash-stripped on the wire
    assert server._confirm_required(payload) is not None                 # confirm FIRES (was None = the bug)
    assert server._confirm_required("/project delete demo") is not None  # slashed form still works (tolerant)
    _, _, gp = commands.classify("/config set ?")
    assert gp == "config set ?" and server._guide_required(gp) is not None
    assert server._guide_required("/lifecycle gate --guide")["command"] == "lifecycle"
    # safe/turn forms still pass through untouched
    assert server._confirm_required(commands.classify("/project list")[2]) is None
    assert server._guide_required(commands.classify("/config set x on")[2]) is None
    assert server._confirm_required("hello there") is None
