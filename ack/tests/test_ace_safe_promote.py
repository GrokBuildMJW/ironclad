"""#1070 (epic #1065): learned-state safety. ACE deltas were applied without an eval gate or snapshot, so a
bad learned delta could silently degrade behavior. safe_promote snapshots the PRE-adapt playbook (an
operator/auto rollback point) and, when an eval scorer is wired, AUTO-REVERTS a measured regression (never
persists a delta that lowered the score). Default-off (byte-identical)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_CORE = Path(__file__).resolve().parents[2]
for p in (str(_CORE), str(_CORE / "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ack.ace as ace_mod            # noqa: E402
from ack.ace.robust import regression_verdict  # noqa: E402
from playbook_store import PlaybookStore        # noqa: E402

SCOPE = "proj::main"


def test_regression_verdict():
    assert regression_verdict(0.8, 0.5)["revert"] is True             # dropped → revert
    assert regression_verdict(0.5, 0.8)["revert"] is False            # improved → keep
    assert regression_verdict(0.5, 0.5)["revert"] is False            # equal → keep
    assert regression_verdict(0.5, 0.49, tolerance=0.05)["revert"] is False   # within tolerance → keep
    assert regression_verdict("x", 0.5)["revert"] is False            # non-numeric → fail-open (keep)


def _store(tmp_path, monkeypatch):
    st = PlaybookStore(tmp_path / "ace")
    st._chat = lambda p: "x"                                          # non-None so adapt runs

    def fake_adapt_once(traj, pb, **kw):
        pb.add_bullet("new-strategy", "strategies_and_hard_rules")
        return {"skipped": False, "added": 1}

    monkeypatch.setattr(ace_mod, "adapt_once", fake_adapt_once)
    return st


def test_adapt_default_off_takes_no_snapshot(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.adapt(object(), scope=SCOPE)
    assert st.versions(SCOPE) == []                                  # no snapshot when safe_promote OFF
    assert any("new-strategy" in x for x in st.get_lessons(SCOPE))    # delta persisted


def test_safe_promote_snapshots_before_adapt(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.configure(safe_promote=True)
    st.adapt(object(), scope=SCOPE)
    assert len(st.versions(SCOPE)) >= 1                              # pre-adapt rollback point exists
    assert any("new-strategy" in x for x in st.get_lessons(SCOPE))    # no eval → promoted


def test_safe_promote_auto_reverts_a_measured_regression(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.configure(safe_promote=True)
    scores = iter([0.8, 0.3])                                        # before 0.8 → after 0.3 (regressed)
    st.set_transports(eval_fn=lambda pb: next(scores))
    r = st.adapt(object(), scope=SCOPE)
    assert r.get("reverted") is True
    assert not any("new-strategy" in x for x in st.get_lessons(SCOPE))   # NOT persisted (auto-reverted)


def test_safe_promote_keeps_an_improvement(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.configure(safe_promote=True)
    scores = iter([0.5, 0.9])                                        # improved
    st.set_transports(eval_fn=lambda pb: next(scores))
    r = st.adapt(object(), scope=SCOPE)
    assert not r.get("reverted") and r["scores"] == {"before": 0.5, "after": 0.9}
    assert any("new-strategy" in x for x in st.get_lessons(SCOPE))    # persisted
