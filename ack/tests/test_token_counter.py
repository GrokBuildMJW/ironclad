"""Epic #366 / P1 (1/3) — token-accurate context budgeting (engine/gx10.py).

Fixes the live HTTP 400 ("maximum context length is 32768", `0 gen · 0 tok`): the trim no longer
guesses chars/4 against a hard TOKEN wall. The seam counts real tokens via the served model's
tokenizer (the vLLM ``/tokenize`` endpoint) and falls back to a CALIBRATED chars/token estimate
only when that endpoint is unreachable. Validated WITHOUT a live model:

  * the tokenizer URL/host heuristics (real route is ``/tokenize`` at the root, not ``/v1/tokenize``);
  * ``_TokenCounter`` live counting + caching + fail-soft-to-fallback on any error;
  * the calibrated char fallback (conservative);
  * ``_rag_block`` budgets in REAL tokens (not chars/4);
  * ``_trim_context`` keeps ``prompt_tokens + max_tokens ≤ max_model_len`` after budgeting on dense
    (code/JSON) AND CJK content — the cases the chars/4 estimate silently overflowed; with a
    negative path (a dead counter / token-budget OFF ⇒ today's char hysteresis).
"""
from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mk_urlopen(count_fn, calls, boom=False):
    def _urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append(body)
        if boom:
            raise RuntimeError("tokenize endpoint down")
        return _Resp({"count": count_fn(body)})
    return _urlopen


class _FakeCounter:
    """A live token counter at a fixed chars/token density (no network). cpt=2 ≈ dense
    code/JSON, cpt=1.2 ≈ CJK. ``dead`` models the endpoint being unavailable."""
    def __init__(self, cpt=2.0, dead=False):
        self.cpt = float(cpt)
        self.dead = bool(dead)

    def usable(self):
        return not self.dead

    def count_text(self, text):
        if self.dead:
            return None
        return int(math.ceil(len(text or "") / self.cpt))

    def count_prompt(self, messages, per_msg_overhead=4):
        if self.dead:
            return None
        total = 0
        for m in messages:
            c = self.count_text(gx10._message_text(m))
            if c is None:
                return None
            total += c + per_msg_overhead
        return total


def _mk_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    return gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")


def _dense_rounds(n, size, fill="x"):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"u{i} " + fill * size})
        out.append({"role": "assistant", "content": f"a{i} " + fill * size})
    return out


# ── URL + host heuristics ────────────────────────────────────────────────────
def test_tokenize_url_strips_v1():
    assert gx10._tokenize_url("http://h:8000/v1") == "http://h:8000/tokenize"
    assert gx10._tokenize_url("http://h:8000/v1/") == "http://h:8000/tokenize"
    assert gx10._tokenize_url("http://h:8000") == "http://h:8000/tokenize"   # no /v1 → root
    assert gx10._tokenize_url("") == ""                                       # empty stays empty


def test_host_is_probeable():
    assert gx10._host_is_probeable("http://203.0.113.5:8000/v1") is True     # a routable IPv4
    assert gx10._host_is_probeable("http://spark.lan:8000/v1") is True       # dotted name
    assert gx10._host_is_probeable("http://localhost:8000/v1") is False      # loopback
    assert gx10._host_is_probeable("http://127.0.0.1:8000/v1") is False      # loopback
    assert gx10._host_is_probeable("http://x/v1") is False                   # bare single-label stub
    assert gx10._host_is_probeable("") is False


# ── _TokenCounter (live + fail-soft) ─────────────────────────────────────────
def test_counter_offline_host_is_dead_pure_fallback():
    c = gx10._TokenCounter("http://x/v1", "m")          # non-probeable host, not forced
    assert c.usable() is False
    assert c.count_text("anything") is None             # never touches the network
    assert c.count_prompt([{"role": "user", "content": "hi"}]) is None


def test_counter_live_counts_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(gx10.urllib.request, "urlopen",
                        _mk_urlopen(lambda b: len(b["prompt"]) // 2, calls))
    c = gx10._TokenCounter("http://x/v1", "m", force_probe=True)   # force probe a stub host
    assert c.usable() is True
    assert c.count_text("hello world!!") == 6           # 13 // 2
    assert c.live is True
    assert len(calls) == 1
    assert c.count_text("hello world!!") == 6           # cached → no extra POST
    assert len(calls) == 1
    assert calls[0]["add_special_tokens"] is False       # a fragment isn't BOS-padded


def test_counter_dies_on_error_then_inert(monkeypatch):
    calls = []
    monkeypatch.setattr(gx10.urllib.request, "urlopen",
                        _mk_urlopen(lambda b: 1, calls, boom=True))
    c = gx10._TokenCounter("http://x/v1", "m", force_probe=True)
    assert c.count_text("x") is None                    # endpoint error
    assert c.usable() is False                           # permanently inert for the session
    assert c.count_text("y") is None
    assert len(calls) == 1                               # never retried after going dead


def test_counter_bad_count_response_goes_dead(monkeypatch):
    # #371 review S3: a 200 without a valid integer `count` (proxy / wrong service / route shape)
    # must NOT be read as 0 tokens — that would leave the counter "usable" and never trim. Treat it
    # as endpoint-dead so the engine drops to the conservative char fallback.
    calls = []

    def _urlopen_nocount(req, timeout=None):
        calls.append(1)
        return _Resp({"tokens": [1, 2, 3]})              # 200 OK, but NO 'count' key

    monkeypatch.setattr(gx10.urllib.request, "urlopen", _urlopen_nocount)
    c = gx10._TokenCounter("http://x/v1", "m", force_probe=True)
    assert c.count_text("hello") is None
    assert c.usable() is False                            # went inert, not a bogus 0
    assert len(calls) == 1                                # never retried after going dead


def test_tools_schema_tokens(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt=2.0))
    monkeypatch.setattr(gx10, "_effective_tools", lambda: [{"a": "x" * 40}])
    assert gx10._tools_schema_tokens() > 0                # counts the serialized schema
    monkeypatch.setattr(gx10, "_effective_tools", lambda: [])
    assert gx10._tools_schema_tokens() == 0              # no tools → no reserve
    monkeypatch.setattr(gx10, "_effective_tools", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gx10._tools_schema_tokens() == 0              # never breaks the trim


def test_count_prompt_sums_with_overhead(monkeypatch):
    calls = []
    monkeypatch.setattr(gx10.urllib.request, "urlopen",
                        _mk_urlopen(lambda b: len(b["prompt"]), calls))   # 1 token per char
    c = gx10._TokenCounter("http://x/v1", "m", force_probe=True)
    msgs = [{"role": "user", "content": "abcd"},        # 4 + 4 overhead
            {"role": "assistant", "content": "ef"}]     # 2 + 4 overhead
    assert c.count_prompt(msgs) == (4 + 4) + (2 + 4)


def test_counter_disabled_via_env_no_counter(monkeypatch, tmp_path):
    monkeypatch.setenv("GX10_TOKENIZE", "0")
    monkeypatch.setenv("GX10_DISCOVER_WINDOW", "0")      # also stop the #377 boot /v1/models probe
    monkeypatch.setattr(gx10, "_TOKENS", None)           # (probeable host here → keep the suite offline)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    monkeypatch.chdir(tmp_path)
    gx10.GX10(base_url="http://203.0.113.5:8000/v1", api_key="k", model="m", prompt_path="")
    assert gx10._TOKENS is None                          # off ⇒ pure char fallback, no counter


def test_counter_forced_via_env_is_usable(monkeypatch, tmp_path):
    monkeypatch.setenv("GX10_TOKENIZE", "1")
    monkeypatch.setattr(gx10, "_TOKENS", None)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    monkeypatch.chdir(tmp_path)
    gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    assert gx10._TOKENS is not None and gx10._TOKENS.usable()   # forced probeable (no network yet)


# ── module budget helpers ────────────────────────────────────────────────────
def test_char_token_estimate_is_conservative():
    # calibrated default 2.6: 26 chars ⇒ 10 tokens (rounds up), never 0 for non-empty
    assert gx10._char_token_estimate("a" * 26) == 10
    assert gx10._char_token_estimate("") == 0
    assert gx10._char_token_estimate("a") == 1


def test_count_tokens_helper_fallback_then_live(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", None)
    assert gx10._count_tokens("a" * 26) == 10            # falls back to the char estimate
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt=2.0))
    assert gx10._count_tokens("abcd") == 2               # live exact (4 // 2)


def test_derive_token_budget_reserve_scale_floor():
    hi, lo = gx10._derive_token_budget(32768, 8192, 1024, 512)
    assert hi == 20736 and lo == int(hi * 0.6)           # (32768-9728)*0.9
    hi64, _ = gx10._derive_token_budget(65536, 8192, 1024, 512)
    assert hi64 > hi                                     # scales with the window
    tiny, _ = gx10._derive_token_budget(1000, 8192, 1024, 512)
    assert tiny == 2048                                  # floored, never ≤0


# ── _rag_block in real tokens ────────────────────────────────────────────────
def test_rag_block_budgets_in_tokens_live(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt=2.0))
    hits = ["A" * 30, "B" * 30]                           # each line ≈ ceil(33/2)=17 tokens
    out = gx10._rag_block(hits, 20)                       # budget fits one, not two
    assert out.startswith(gx10._RAG_MARKER)
    assert ("A" * 30) in out and ("B" * 30) not in out
    assert out.count("- ") == 1


def test_rag_block_budgets_in_tokens_fallback(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", None)            # no live counter → calibrated chars/token
    hits = ["A" * 30, "B" * 30]
    out = gx10._rag_block(hits, 20)
    assert ("A" * 30 in out) and ("B" * 30 not in out)   # still one line under the token budget
    assert out.count("- ") == 1


def test_rag_block_empty_on_zero_budget(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt=2.0))
    assert gx10._rag_block(["x" * 100], 0) == ""          # nothing fits a zero budget


# ── _trim_context token-accurate (the DoD) ───────────────────────────────────
def _setup_small_window(monkeypatch, tools_tok=0):
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", True)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", False)   # plain drop → no summarizer model call
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", 4096)
    monkeypatch.setattr(gx10, "MAX_TOKENS", 512)
    monkeypatch.setattr(gx10, "RAG_MAX_TOKENS", 128)
    monkeypatch.setattr(gx10, "SUMMARY_MAX_TOKENS", 64)
    # by default isolate the message-budget tests from the (large, config-dependent) real tools
    # schema; a dedicated test exercises the tools reserve.
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: tools_tok)


def _mk_small_agent(monkeypatch, tmp_path, max_tokens=512):
    g = _mk_agent(monkeypatch, tmp_path)
    g.max_tokens = max_tokens                              # the value the REQUEST uses (#371 S3)
    return g


def test_trim_token_accurate_dense_fits_the_wall(monkeypatch, tmp_path):
    _setup_small_window(monkeypatch)
    counter = _FakeCounter(cpt=2.0)                        # dense code/JSON ≈ 2 c/t
    monkeypatch.setattr(gx10, "_TOKENS", counter)
    g = _mk_small_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"}] + _dense_rounds(40, 220)
    pre = counter.count_prompt(g.messages)

    g._trim_context()

    post = counter.count_prompt(g.messages)
    assert post < pre                                              # it actually trimmed
    # fits the wall after budgeting — including the tools reserve + the request's max_tokens (DoD)
    assert post + gx10._tools_schema_tokens() + g.max_tokens <= gx10.MAX_MODEL_LEN
    assert any(m.get("role") == "user" for m in g.messages)      # never dropped the last user turn
    assert g.messages[0]["content"] == "SYS"                     # system prompt preserved


def test_trim_token_accurate_cjk_fits_the_wall(monkeypatch, tmp_path):
    # CJK is where chars/4 fails hardest (~1.2 c/t ⇒ chars/4 under-counts ~3×). The token-accurate
    # trim must still bring the window under the wall.
    _setup_small_window(monkeypatch)
    counter = _FakeCounter(cpt=1.2)
    monkeypatch.setattr(gx10, "_TOKENS", counter)
    g = _mk_small_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"}] + _dense_rounds(40, 200, fill="你")

    g._trim_context()

    assert counter.count_prompt(g.messages) + g.max_tokens <= gx10.MAX_MODEL_LEN
    assert any(m.get("role") == "user" for m in g.messages)


def test_trim_no_op_below_high_water(monkeypatch, tmp_path):
    _setup_small_window(monkeypatch)
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt=2.0))
    g = _mk_small_agent(monkeypatch, tmp_path)
    small = [{"role": "system", "content": "SYS"}] + _dense_rounds(1, 20)
    g.messages = list(small)
    g._trim_context()
    assert g.messages == small                                   # under budget ⇒ prefix cache stays


def test_trim_reserves_tools_schema(monkeypatch, tmp_path):
    # #371 review S2: the tools schema vLLM serializes into the prompt MUST be reserved by the trim,
    # else a dense tool set overflows the wall even when the message trim "fits". With a 2200-token
    # tools reserve the trim must drop more — the old code (no tools term) left ~`low` tokens and
    # 1831+2200+512 > 4096 overflowed.
    _setup_small_window(monkeypatch, tools_tok=2200)
    counter = _FakeCounter(cpt=2.0)
    monkeypatch.setattr(gx10, "_TOKENS", counter)
    g = _mk_small_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"}] + _dense_rounds(40, 220)

    g._trim_context()

    final = counter.count_prompt(g.messages)
    assert final + gx10._tools_schema_tokens() + g.max_tokens <= gx10.MAX_MODEL_LEN   # tools reserved
    assert any(m.get("role") == "user" for m in g.messages)


def test_trim_returns_false_when_system_partition_too_big(monkeypatch, tmp_path):
    # #371 review S2: a system partition (+ tools + output) that alone exceeds the wall must NOT be
    # certified as a fit — the floors used to do that. _trim_context_tokens returns False so the
    # dispatcher falls back (and ultimately the #372 pre-flight guard raises ContextOverflowError).
    _setup_small_window(monkeypatch)
    counter = _FakeCounter(cpt=2.0)
    monkeypatch.setattr(gx10, "_TOKENS", counter)
    g = _mk_small_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "S" * 7600},          # ≈ 3804 tokens, > the room
                  {"role": "user", "content": "the irreducible last turn"}]
    assert g._trim_context_tokens(counter) is False                  # honest: it cannot certify a fit
    g._trim_context()                                                # dispatcher must not raise


def test_trim_reserves_self_max_tokens_not_module_global(monkeypatch, tmp_path):
    # #371 review S3: the trim must reserve the value the REQUEST uses (self.max_tokens), not the
    # module global — they desync on a runtime `config set generation.max_tokens`.
    _setup_small_window(monkeypatch)
    monkeypatch.setattr(gx10, "MAX_TOKENS", 256)                      # module global (the desynced one)
    counter = _FakeCounter(cpt=2.0)
    monkeypatch.setattr(gx10, "_TOKENS", counter)
    g = _mk_small_agent(monkeypatch, tmp_path, max_tokens=3500)       # the request actually reserves 3500
    g.messages = [{"role": "system", "content": "SYS"}] + _dense_rounds(20, 200)

    g._trim_context()

    # the window must fit the wall against the REQUEST's reserve (3500), not the module global (256).
    # On the buggy (module-global) code this overflows: it would leave ~`low` tokens + 3500 > 4096.
    assert counter.count_prompt(g.messages) + g.max_tokens <= gx10.MAX_MODEL_LEN


def test_trim_falls_back_to_char_path_when_counter_dead(monkeypatch, tmp_path):
    # a dead/inert counter ⇒ the dispatcher uses today's calibrated char hysteresis (negative path)
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", True)
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(dead=True))
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", False)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"}] + _dense_rounds(8, 90)
    g._trim_context()
    others = sum(len(m["content"]) for m in g.messages if m.get("role") != "system")
    assert others <= 1000                                        # char watermark applied, not tokens


def test_trim_token_budget_off_uses_char_path(monkeypatch, tmp_path):
    # TOKEN_BUDGET=False ⇒ never the token path, even with a live counter present
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", False)
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt=2.0))   # live, but ignored when OFF
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", False)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"}] + _dense_rounds(8, 90)
    g._trim_context()
    others = sum(len(m["content"]) for m in g.messages if m.get("role") != "system")
    assert others <= 1000
