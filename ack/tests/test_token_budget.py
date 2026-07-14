"""MEM-9 / §3-Mechanismus 3 — token-accurate budgeting (engine/gx10.py).

The trim watermark is coupled to the MODEL window instead of fixed chars: `_derive_ctx_budget`
turns ``max_model_len`` minus the reserves (output + RAG + summary) into the char high/low-water
(_trim_context still measures chars; tokens ≈ chars/CHARS_PER_TOKEN). Validated as a pure function
plus the env wiring + one _apply_config integration (globals restored).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


# ── _derive_ctx_budget (pure) ────────────────────────────────────────────────
def test_derive_reserves_and_hysteresis():
    hi, lo = gx10._derive_ctx_budget(32768, 8192, 1024, 512, 4)
    # reserve = 9728; budget_tok = int((32768-9728)*0.9) = 20736; high = 20736*4
    assert hi == 82944
    assert lo == int(hi * 0.6)  # 60% low-water (mirrors the legacy 80k→48k hysteresis)


def test_derive_scales_with_window():
    hi32, _ = gx10._derive_ctx_budget(32768, 8192, 1024, 512, 4)
    hi64, _ = gx10._derive_ctx_budget(65536, 8192, 1024, 512, 4)
    assert hi64 > hi32  # a bigger model window → a bigger working set


def test_derive_more_reserve_means_smaller_budget():
    base, _ = gx10._derive_ctx_budget(32768, 8192, 1024, 512, 4)
    more, _ = gx10._derive_ctx_budget(32768, 16384, 1024, 512, 4)  # larger output reserve
    assert more < base  # reserving more for output leaves less working set


def test_derive_floored_on_tiny_window():
    hi, lo = gx10._derive_ctx_budget(1000, 8192, 1024, 512, 4)
    assert hi == 2048 * 4 and lo == int(hi * 0.6)  # never collapses to ≤0


# ── env wiring ───────────────────────────────────────────────────────────────
def test_env_wires_max_model_len_and_token_budget(monkeypatch):
    monkeypatch.setenv("GX10_MAX_MODEL_LEN", "65536")
    monkeypatch.setenv("GX10_TOKEN_BUDGET", "0")
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["context"]["max_model_len"] == 65536
    assert cfg["context"]["token_budget"] is False


def test_env_ironclad_fallback(monkeypatch):
    monkeypatch.delenv("GX10_MAX_MODEL_LEN", raising=False)
    monkeypatch.setenv("IRONCLAD_MAX_MODEL_LEN", "49152")  # the deploy/vLLM var
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["context"]["max_model_len"] == 49152


def test_env_gx10_overrides_ironclad(monkeypatch):
    monkeypatch.setenv("IRONCLAD_MAX_MODEL_LEN", "49152")
    monkeypatch.setenv("GX10_MAX_MODEL_LEN", "65536")  # more specific → wins
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["context"]["max_model_len"] == 65536


# ── #379: the output reserve (MAX_TOKENS) is a tunable default (kept at 8192) ─────────────────
def test_max_tokens_default_is_8192():
    # #379/C-5: the output (generation) token reserve default. Kept at 8192 (PERF-10: 4096 truncated
    # long handovers); tunable for more context headroom (test below).
    assert gx10._code_defaults()["generation"]["max_tokens"] == 8192


def test_env_wires_max_tokens(monkeypatch):
    monkeypatch.setenv("GX10_MAX_TOKENS", "4096")          # lower the reserve → more context headroom
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["generation"]["max_tokens"] == 4096


# ── _apply_config integration (globals restored) ─────────────────────────────
def test_apply_config_derives_when_on_and_respects_off():
    saved = (gx10.MAX_CTX_CHARS, gx10.TRIM_TARGET_CHARS, gx10.MAX_MODEL_LEN,
             gx10.TOKEN_BUDGET, gx10.MAX_TOKENS, gx10.RAG_MAX_TOKENS, gx10.SUMMARY_MAX_TOKENS)
    try:
        on = gx10._code_defaults()
        on["context"].update({"token_budget": True, "max_model_len": 32768,
                              "rag_max_tokens": 1024, "summary_max_tokens": 512})
        on["generation"]["max_tokens"] = 8192
        gx10._apply_config(on)
        # derived from the window via the CALIBRATED fallback ratio (2.6, #366): int(20736*2.6).
        # (The fixed 4 c/t was the live overflow; the live tokenizer is exact, this is the fallback.)
        assert gx10.MAX_CTX_CHARS == 53913

        off = gx10._code_defaults()
        off["context"].update({"token_budget": False, "max_ctx_chars": 80000})
        gx10._apply_config(off)
        assert gx10.MAX_CTX_CHARS == 80000  # off → the configured char value stands
    finally:
        (gx10.MAX_CTX_CHARS, gx10.TRIM_TARGET_CHARS, gx10.MAX_MODEL_LEN,
         gx10.TOKEN_BUDGET, gx10.MAX_TOKENS, gx10.RAG_MAX_TOKENS, gx10.SUMMARY_MAX_TOKENS) = saved


# ── #503 BUDGET-1/2/3: char-fallback trim hardening ───────────────────────────
def test_char_trim_reserves_system_tools_thinking(monkeypatch):
    # BUDGET-1: the char-fallback watermark must reserve sys+tools+thinking; the same `others` that fits
    # the bare MAX_CTX_CHARS must trim once a large system prompt + tools + thinking are reserved.
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 10000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 6000)
    monkeypatch.setattr(gx10, "THINKING_RESERVE", 1000)
    monkeypatch.setattr(gx10, "CHARS_PER_TOKEN", 1.0)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", False)
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: 500)   # → 500 chars at cpt 1.0
    msgs = [{"role": "system", "content": "S" * 4000},               # reserve = 4000+500+1000 = 5500
            {"role": "user", "content": "U" * 3000},
            {"role": "assistant", "content": "A" * 3000},            # round 1 = 6000
            {"role": "user", "content": "Q" * 200},
            {"role": "assistant", "content": "B" * 200}]             # round 2 = 400  (others = 6400)
    fake = types.SimpleNamespace(messages=list(msgs))
    gx10.GX10._trim_context_chars(fake)                              # high = 10000-5500 = 4500 < 6400 → trims
    others = [m for m in fake.messages if m.get("role") != "system"]
    assert len(others) < 4 and sum(len(gx10._message_text(m)) for m in others) <= int(4500 * 0.6)
    assert any(m.get("role") == "system" for m in fake.messages)    # system partition preserved
    # contrast: with NO reserve (tiny system, no tools/thinking) the same others fit → no trim
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: 0)
    monkeypatch.setattr(gx10, "THINKING_RESERVE", 0)
    keep = [{"role": "system", "content": ""}] + msgs[1:]
    fake2 = types.SimpleNamespace(messages=list(keep))
    gx10.GX10._trim_context_chars(fake2)
    assert len([m for m in fake2.messages if m.get("role") != "system"]) == 4   # 6400 <= 10000 → unchanged


def test_char_trim_counts_tool_call_arguments(monkeypatch):
    # BUDGET-2: an assistant tool-call message has empty content but large `arguments`; the char trim must
    # count them (via _message_text) — a content-only sum under-measured and never trimmed a tool-heavy round.
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 3000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 1500)
    monkeypatch.setattr(gx10, "THINKING_RESERVE", 0)
    monkeypatch.setattr(gx10, "CHARS_PER_TOKEN", 1.0)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", False)
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: 0)
    msgs = [{"role": "system", "content": ""},
            {"role": "user", "content": "U" * 100},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": "X" * 4000}}]},
            {"role": "tool", "content": "ok"},
            {"role": "user", "content": "Q"}]                        # content-only others ~= 103 (would NOT trim)
    fake = types.SimpleNamespace(messages=list(msgs))
    gx10.GX10._trim_context_chars(fake)                              # _message_text sees ~4001 → > 3000 → trims
    others = [m for m in fake.messages if m.get("role") != "system"]
    assert not any(m.get("tool_calls") for m in others)             # the big-arguments round was evicted


def test_apply_config_honors_operator_supplied_char_budget(monkeypatch):
    # BUDGET-3: an operator-set GX10_MAX_CTX_CHARS must be honored, not silently overwritten by the derive.
    saved = (gx10.MAX_CTX_CHARS, gx10.TRIM_TARGET_CHARS, gx10.MAX_MODEL_LEN,
             gx10.TOKEN_BUDGET, gx10.MAX_TOKENS, gx10.RAG_MAX_TOKENS, gx10.SUMMARY_MAX_TOKENS)
    try:
        monkeypatch.setenv("GX10_MAX_CTX_CHARS", "12345")
        cfg = gx10._code_defaults()
        cfg["context"].update({
            "token_budget": True,
            "max_model_len": 32768,
            "max_ctx_chars": 12345,
            "trim_target_chars": 10000,
        })
        gx10._apply_config(cfg)
        assert gx10.MAX_CTX_CHARS == 12345   # operator env honored despite token_budget on (no clobber)
    finally:
        (gx10.MAX_CTX_CHARS, gx10.TRIM_TARGET_CHARS, gx10.MAX_MODEL_LEN,
         gx10.TOKEN_BUDGET, gx10.MAX_TOKENS, gx10.RAG_MAX_TOKENS, gx10.SUMMARY_MAX_TOKENS) = saved
