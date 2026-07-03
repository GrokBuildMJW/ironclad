"""#1074 (epic #1043 quick-win): the bounded, trust-gated, SSRF-guarded fetch_url tool.

web_search FINDS pages; fetch_url READS a specific http(s) page verbatim (RFCs/standards/API specs), byte-
and char-capped. Offered only when the trust profile allows outbound (blocked under sealed); refuses non-
public hosts so an autonomous agent cannot pivot to internal services.
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


def test_ssrf_guard_blocks_local_private_and_nonhttp():
    assert gx10._fetch_url_blocked("ftp://host/x")          # non-http scheme
    assert gx10._fetch_url_blocked("file:///etc/passwd")    # non-http scheme
    assert gx10._fetch_url_blocked("notaurl")               # no scheme/host
    assert gx10._fetch_url_blocked("http://localhost/x")    # loopback host name
    assert gx10._fetch_url_blocked("http://127.0.0.1/x")    # loopback ip (resolved offline — literal)
    assert gx10._fetch_url_blocked("http://10.0.0.5/x")     # private
    assert gx10._fetch_url_blocked("http://169.254.169.254/latest/meta-data")  # link-local (cloud metadata)


def test_registered_only_when_trust_allows_outbound(monkeypatch):
    monkeypatch.setattr(gx10, "_web_search_trust_ok", lambda: True)
    assert "fetch_url" in {t["function"]["name"] for t in gx10._effective_tools()}
    monkeypatch.setattr(gx10, "_web_search_trust_ok", lambda: False)   # sealed
    assert "fetch_url" not in {t["function"]["name"] for t in gx10._effective_tools()}


def test_executor_blocked_under_sealed(monkeypatch):
    monkeypatch.setattr(gx10, "_web_search_trust_ok", lambda: False)
    out = gx10.run_tool("fetch_url", {"url": "https://example.com"})
    assert out.startswith("BLOCKED") and "sealed" in out


class _Resp:
    def __init__(self, data, ctype="text/plain"):
        self._d = data
        self.headers = {"Content-Type": ctype}

    def read(self, n):
        return self._d[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_returns_body_and_caps_oversize(monkeypatch):
    monkeypatch.setattr(gx10, "_web_search_trust_ok", lambda: True)
    monkeypatch.setattr(gx10, "_fetch_url_blocked", lambda _u: None)          # skip DNS in the unit test
    monkeypatch.setattr(gx10.urllib.request, "urlopen", lambda req, timeout=None: _Resp(b"HELLO WORLD"))
    out = gx10.run_tool("fetch_url", {"url": "https://example.com/spec"})
    assert "HELLO WORLD" in out and "fetch_url https://example.com/spec" in out
    # oversize download → truncation marker
    monkeypatch.setattr(gx10, "_FETCH_MAX_BYTES", 5)
    monkeypatch.setattr(gx10.urllib.request, "urlopen", lambda req, timeout=None: _Resp(b"X" * 100))
    out2 = gx10.run_tool("fetch_url", {"url": "https://example.com/big"})
    assert "truncated" in out2 and out2.count("X") == 5


def test_fetch_url_is_an_ingestion_tool_so_the_choke_point_caps_it():
    assert "fetch_url" in gx10._INGESTION_TOOLS          # its result is capped to the live per-turn budget
