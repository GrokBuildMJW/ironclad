"""#602 2.7 / #808 — engine integration tests for the Quality-breaker CONSUMER on `post_handover`.

Proves the closed segment Verifier→score→Quality actually runs on the dev-task pipeline: with
`verify.enabled` + `quality.enabled`, staging a handover feeds the mark-only Verifier score into the
quality breaker and surfaces a sustained-degradation trip. MARK-ONLY (never gates) + default-off
byte-identical (no consumer hook registered).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10
from ack import hooks


class _FakeMem:
    def __init__(self, hits):
        self._hits = list(hits)

    def is_available(self):
        return True

    def search(self, q, limit=5):
        return list(self._hits)


_GOOD = ('{"type":"feature","priority":"high","title":"Build the order service",'
         '"description":"Implement the full order service with validation, persistence and tests."}')
_THIN = '{"type":"feature","priority":"high","title":"Fix","description":"do it"}'   # fails both rules → score 0
_HANDOVER = "## Handover\nbuild the order service end to end with full validation and tests"


@pytest.fixture(autouse=True)
def _reset():
    hooks.clear_hooks()
    gx10._set_last_verdict(None)
    gx10._QUALITY_TRIPPED = None
    yield
    hooks.clear_hooks()
    gx10._set_last_verdict(None)
    gx10._QUALITY_TRIPPED = None
    gx10._apply_config(gx10._code_defaults())   # restore defaults → verify+quality off → hooks unregistered


def _stage(tmp_path, monkeypatch, *, verify_on, quality_on, mem=None, task_json=None, min_consecutive=1):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    cfg = gx10._code_defaults()
    if verify_on:
        cfg["verify"]["enabled"] = True
    if quality_on:
        cfg["quality"]["enabled"] = True
        cfg["quality"]["min_consecutive"] = min_consecutive
        cfg["quality"]["threshold"] = 0.5
    gx10.STORE = None
    gx10._apply_config(cfg)
    gx10._dispatch(types.SimpleNamespace(run=lambda t: None, save_session=lambda: None, status=lambda: "ok"),
                   "initiative new Order Service --type software")
    return gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=task_json or _GOOD, force=True)


def test_low_verdict_score_trips_the_breaker(tmp_path, monkeypatch):
    out = _stage(tmp_path, monkeypatch, verify_on=True, quality_on=True, mem=None, task_json=_THIN, min_consecutive=1)
    assert out.startswith("OK")                       # MARK-ONLY: the handover still succeeds
    snap = gx10._quality_tripped()
    assert snap is not None and snap.tripped          # a 0.0 verdict score (< 0.5) tripped the fed breaker


def test_good_verdict_does_not_trip(tmp_path, monkeypatch):
    out = _stage(tmp_path, monkeypatch, verify_on=True, quality_on=True, mem=_FakeMem(["hit"]), min_consecutive=1)
    assert out.startswith("OK")
    assert gx10._quality_tripped() is None            # score 1.0 >= threshold → recorded, not tripped


def test_default_off_no_consumer_byte_identical(tmp_path, monkeypatch):
    out = _stage(tmp_path, monkeypatch, verify_on=True, quality_on=False, mem=None, task_json=_THIN)
    assert out.startswith("OK")
    assert hooks.hook_count("post_handover") == 0     # no consumer hook registered
    assert gx10._quality_tripped() is None


def test_disable_unregisters_the_consumer(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch, verify_on=True, quality_on=True, mem=_FakeMem(["hit"]))
    assert hooks.hook_count("post_handover") == 1
    gx10._apply_config(gx10._code_defaults())         # quality.enabled off
    assert hooks.hook_count("post_handover") == 0     # cleanly unregistered
