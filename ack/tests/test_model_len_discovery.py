"""Epic #366 / P2 (#377) — live max_model_len discovery from /v1/models at startup.

The engine reads the served model window from ``GET /v1/models`` at boot and adopts it (fail-soft →
keep the configured ``MAX_MODEL_LEN``), so the token budget can't drift when the Spark is relaunched
with a different ``--max-model-len`` (finding C-4). Validated WITHOUT a live model:

  * ``_discover_max_model_len`` parses the live window (the matching model, else the first); fail-soft
    (network error / malformed payload ⇒ None);
  * ``GX10.__init__`` adopts a differing live window and re-derives the char-fallback watermarks;
  * skipped on a non-probeable host (hermetic), when ``GX10_DISCOVER_WINDOW=0``, and on any failure
    (the configured window stands) — the negative paths.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mk_models_urlopen(window, model="qwen3.6-35b", calls=None):
    def _urlopen(url, timeout=None):
        if calls is not None:
            calls.append(url)
        return _Resp({"data": [{"id": model, "max_model_len": window}]})
    return _urlopen


def _boom_urlopen(url, timeout=None):
    raise RuntimeError("models endpoint down")


# ── _discover_max_model_len (pure-ish) ───────────────────────────────────────
def test_discover_returns_matching_model_window(monkeypatch):
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _mk_models_urlopen(49152, "qwen3.6-35b"))
    assert gx10._discover_max_model_len("http://h.test:8000/v1", "qwen3.6-35b") == 49152


def test_discover_falls_back_to_first_entry(monkeypatch):
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _mk_models_urlopen(40960, "served-model"))
    assert gx10._discover_max_model_len("http://h.test:8000/v1", "other-model") == 40960


def test_discover_url_is_v1_models(monkeypatch):
    calls = []
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _mk_models_urlopen(32768, calls=calls))
    gx10._discover_max_model_len("http://h.test:8000/v1", "m")
    assert calls == ["http://h.test:8000/v1/models"]


def test_discover_fail_soft_on_error(monkeypatch):
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _boom_urlopen)
    assert gx10._discover_max_model_len("http://h.test:8000/v1", "m") is None


def test_discover_fail_soft_on_malformed(monkeypatch):
    monkeypatch.setattr(gx10.urllib.request, "urlopen", lambda url, timeout=None: _Resp({"data": [{}]}))
    assert gx10._discover_max_model_len("http://h.test:8000/v1", "m") is None    # no max_model_len
    monkeypatch.setattr(gx10.urllib.request, "urlopen", lambda url, timeout=None: _Resp({}))
    assert gx10._discover_max_model_len("http://h.test:8000/v1", "m") is None    # no data


# ── GX10.__init__ adoption ───────────────────────────────────────────────────
def _prep_globals(monkeypatch, window=32768):
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", window)
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", True)
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 80000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 48000)
    monkeypatch.setattr(gx10, "_TOKENS", None)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    monkeypatch.delenv("GX10_DISCOVER_WINDOW", raising=False)


def _mk(monkeypatch, tmp_path, host):
    monkeypatch.chdir(tmp_path)
    return gx10.GX10(base_url=host, api_key="k", model="qwen3.6-35b", prompt_path="")


def test_init_adopts_differing_live_window_and_rederives(monkeypatch, tmp_path):
    _prep_globals(monkeypatch, window=32768)
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _mk_models_urlopen(49152))
    _mk(monkeypatch, tmp_path, "http://h.test:8000/v1")                 # probeable (dotted) host
    assert gx10.MAX_MODEL_LEN == 49152                                  # adopted the live window
    expect_hi, _ = gx10._derive_ctx_budget(49152, gx10.MAX_TOKENS, gx10.RAG_MAX_TOKENS,
                                           gx10.SUMMARY_MAX_TOKENS, gx10.CHARS_PER_TOKEN)
    assert gx10.MAX_CTX_CHARS == expect_hi                              # watermarks re-derived


def test_init_no_discovery_on_nonprobeable_host(monkeypatch, tmp_path):
    _prep_globals(monkeypatch, window=32768)
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _boom_urlopen)  # would raise if ever called
    _mk(monkeypatch, tmp_path, "http://x/v1")                          # stub host ⇒ no discovery
    assert gx10.MAX_MODEL_LEN == 32768                                  # unchanged, no network


def test_init_discovery_disabled_via_env(monkeypatch, tmp_path):
    _prep_globals(monkeypatch, window=32768)
    monkeypatch.setenv("GX10_DISCOVER_WINDOW", "0")
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _mk_models_urlopen(49152))
    _mk(monkeypatch, tmp_path, "http://h.test:8000/v1")               # probeable but disabled
    assert gx10.MAX_MODEL_LEN == 32768                                  # not adopted


def test_init_fail_soft_keeps_configured_window(monkeypatch, tmp_path):
    _prep_globals(monkeypatch, window=32768)
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _boom_urlopen)  # discovery fails
    _mk(monkeypatch, tmp_path, "http://h.test:8000/v1")
    assert gx10.MAX_MODEL_LEN == 32768                                  # configured window stands


def test_init_same_window_is_noop(monkeypatch, tmp_path):
    _prep_globals(monkeypatch, window=32768)
    monkeypatch.setattr(gx10.urllib.request, "urlopen", _mk_models_urlopen(32768))   # same value
    _mk(monkeypatch, tmp_path, "http://h.test:8000/v1")
    assert gx10.MAX_MODEL_LEN == 32768
    assert gx10.MAX_CTX_CHARS == 80000                                 # untouched (no re-derive)
