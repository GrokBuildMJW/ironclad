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
