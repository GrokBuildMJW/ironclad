"""#1465: ACE adaptation is an always-on promote-or-quarantine transaction."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_CORE = Path(__file__).resolve().parents[2]
for p in (str(_CORE), str(_CORE / "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ack.ace as ace_mod                         # noqa: E402
import gx10                                       # noqa: E402
import playbook_store as store_mod                # noqa: E402
from ack.ace.robust import regression_verdict     # noqa: E402
from playbook_store import PlaybookStore          # noqa: E402

SCOPE = "proj::main"


def test_regression_verdict():
    assert regression_verdict(0.8, 0.5)["revert"] is True
    assert regression_verdict(0.5, 0.8)["revert"] is False
    assert regression_verdict(0.5, 0.5)["revert"] is False
    assert regression_verdict(0.5, 0.49, tolerance=0.05)["revert"] is False
    assert regression_verdict("x", 0.5)["revert"] is False


def _store(tmp_path, monkeypatch):
    st = PlaybookStore(tmp_path / "ace")
    st._chat = lambda p: "x"                          # non-None so adapt runs

    def fake_adapt_once(traj, pb, **kw):
        pb.add_bullet("new-strategy", "strategies_and_hard_rules")
        return {"skipped": False, "added": 1}

    monkeypatch.setattr(ace_mod, "adapt_once", fake_adapt_once)
    return st


def test_legacy_false_cannot_disable_snapshot_or_promotion(tmp_path, monkeypatch):
    cfg = gx10._code_defaults()
    cfg["ace"]["safe_promote"] = False
    gx10._consume_config_tombstones(cfg)
    assert "safe_promote" not in cfg["ace"]

    st = _store(tmp_path, monkeypatch)
    with pytest.raises(TypeError):
        st.configure(safe_promote=True)
    result = st.adapt(object(), scope=SCOPE)

    assert len(st.versions(SCOPE)) >= 1
    assert result["promoted"] is True
    assert any("new-strategy" in lesson for lesson in st.get_lessons(SCOPE))


def test_safe_promote_config_is_a_tombstone_and_runtime_set_is_refused(monkeypatch, capsys):
    cfg = gx10._code_defaults()
    assert "safe_promote" not in cfg["ace"]
    cfg["ace"]["safe_promote"] = False

    gx10._apply_config(cfg)
    gx10._apply_config(cfg)
    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1
    assert "ace.safe_promote" in warnings[0] and "always on" in warnings[0]
    assert "safe_promote" not in cfg["ace"]

    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set ace.safe_promote false")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
    assert "safe_promote" not in cfg["ace"]


def test_safe_promote_snapshots_before_adapt(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    result = st.adapt(object(), scope=SCOPE)

    assert len(st.versions(SCOPE)) >= 1
    assert result["promoted"] is True
    assert any("new-strategy" in lesson for lesson in st.get_lessons(SCOPE))


def test_safe_promote_quarantines_a_measured_regression(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    scores = iter([0.8, 0.3])
    st.set_transports(eval_fn=lambda pb: next(scores))

    result = st.adapt(object(), scope=SCOPE)

    assert result["quarantined"] is True
    assert result["promoted"] is False
    assert not any("new-strategy" in lesson for lesson in st.get_lessons(SCOPE))
    records = st.quarantined(SCOPE)
    assert len(records) == 1
    assert records[0]["state"] == "regression"
    assert records[0]["reason"] == "measured regression"
    assert records[0]["scores"] == {"before": 0.8, "after": 0.3}
    assert records[0]["scope_hash"] == st._path(SCOPE).stem
    assert records[0]["source_version"] in st.versions(SCOPE)
    assert records[0]["candidate"]["sections"]["strategies_and_hard_rules"][0]["content"] == "new-strategy"
    assert isinstance(records[0]["timestamp"], float)


def test_safe_promote_keeps_an_improvement(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)

    def noisy_adapt_once(traj, pb, **kw):
        pb.add_bullet("new-strategy", "strategies_and_hard_rules")
        return {"skipped": False, "added": 1, "quarantined": 1}

    monkeypatch.setattr(ace_mod, "adapt_once", noisy_adapt_once)
    scores = iter([0.5, 0.9])
    st.set_transports(eval_fn=lambda pb: next(scores))

    result = st.adapt(object(), scope=SCOPE)

    assert result["promoted"] is True
    assert result["quarantined"] is False
    assert result["scores"] == {"before": 0.5, "after": 0.9}
    assert any("new-strategy" in lesson for lesson in st.get_lessons(SCOPE))


def test_default_gate_promotes_a_normal_addition(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)

    result = st.adapt(object(), scope=SCOPE)

    assert result["promoted"] is True
    assert not result.get("quarantined")
    assert "scores" not in result
    assert any("new-strategy" in lesson for lesson in st.get_lessons(SCOPE))


def test_default_gate_promotes_a_harmful_rating(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.report_lesson(SCOPE, "failed-strategy")

    def mark_harmful(traj, pb, **kw):
        pb.bullets()[0].mark_harmful()
        return {"skipped": False, "rated": 1}

    monkeypatch.setattr(ace_mod, "adapt_once", mark_harmful)
    result = st.adapt(object(), scope=SCOPE)

    promoted = st._load(SCOPE).bullets()[0]
    assert result["promoted"] is True
    assert not result.get("quarantined")
    assert "scores" not in result
    assert promoted.harmful_count == 1
    assert promoted.net_utility == -1


def test_default_gate_quarantines_a_catastrophic_adaptation(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.report_lesson(SCOPE, "known-good-a")
    st.report_lesson(SCOPE, "known-good-b")
    active_before = st._load(SCOPE).to_json()

    def empty_playbook(traj, pb, **kw):
        for bullet in pb.bullets():
            assert pb.remove(bullet.id)
        return {"skipped": False, "pruned": 2}

    monkeypatch.setattr(ace_mod, "adapt_once", empty_playbook)
    result = st.adapt(object(), scope=SCOPE)

    assert result["promoted"] is False
    assert result["quarantined"] is True
    assert "scores" not in result
    assert st._load(SCOPE).to_json() == active_before
    records = st.quarantined(SCOPE)
    assert len(records) == 1
    assert records[0]["state"] == "destructive"
    assert records[0]["reason"] == "catastrophic playbook loss under a non-evaluated adaptation"


def test_evaluator_error_quarantines_candidate_without_changing_active(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.report_lesson(SCOPE, "known-good")

    def broken_evaluator(pb):
        raise RuntimeError("evaluation unavailable")

    st.set_transports(eval_fn=broken_evaluator)
    result = st.adapt(object(), scope=SCOPE)

    assert result["promoted"] is False
    assert result["quarantined"] is True
    assert st.get_lessons(SCOPE) == ["known-good"]
    records = st.quarantined(SCOPE)
    assert records[0]["state"] == "unpromoted"
    assert records[0]["reason"] == "evaluation unavailable"
    assert records[0]["scores"] is None


@pytest.mark.parametrize("score", [None, True, "0.5"])
def test_non_numeric_evaluator_score_is_unavailable(tmp_path, monkeypatch, score):
    st = _store(tmp_path, monkeypatch)
    st.set_transports(eval_fn=lambda pb: score)

    result = st.adapt(object(), scope=SCOPE)

    assert result["promoted"] is False
    assert result["quarantined"] is True
    assert st.get_lessons(SCOPE) == []
    assert st.quarantined(SCOPE)[0]["state"] == "unpromoted"


@pytest.mark.parametrize("score", [float("nan"), float("inf")])
def test_non_finite_evaluator_score_is_unavailable(tmp_path, monkeypatch, score):
    st = _store(tmp_path, monkeypatch)
    st.set_transports(eval_fn=lambda pb: score)

    result = st.adapt(object(), scope=SCOPE)

    assert result.get("quarantined") is True
    assert result.get("promoted") is False
    assert not any("new-strategy" in lesson for lesson in st.get_lessons(SCOPE))
    records = st.quarantined(SCOPE)
    assert records
    assert records[0]["state"] == "unpromoted"


def test_quarantine_retention_keeps_only_newest_candidates(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    counter = iter(range(store_mod._HISTORY_MAX + 3))

    def add_numbered_candidate(traj, pb, **kw):
        pb.add_bullet(f"candidate-{next(counter)}", "strategies_and_hard_rules")
        return {"skipped": False, "added": 1}

    monkeypatch.setattr(ace_mod, "adapt_once", add_numbered_candidate)
    st.set_transports(eval_fn=lambda pb: 1 if len(pb) == 0 else 0)

    for _ in range(store_mod._HISTORY_MAX + 3):
        assert st.adapt(object(), scope=SCOPE)["quarantined"] is True

    records = st.quarantined(SCOPE)
    assert len(records) == store_mod._HISTORY_MAX
    contents = [record["candidate"]["sections"]["strategies_and_hard_rules"][0]["content"]
                for record in records]
    assert contents[0] == "candidate-3"
    assert contents[-1] == f"candidate-{store_mod._HISTORY_MAX + 2}"


def test_snapshot_failure_refuses_adaptation_without_mutation(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.report_lesson(SCOPE, "known-good")
    active_before = st._load(SCOPE).to_json()
    calls = 0

    def fail_first_snapshot(scope, hist):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("history unavailable")
        return True

    monkeypatch.setattr(st, "_save_history", fail_first_snapshot)
    result = st.adapt(object(), scope=SCOPE)

    assert result["skipped"] is True
    assert result["snapshot_failed"] is True
    assert st._load(SCOPE).to_json() == active_before
    assert st.quarantined(SCOPE) == []


def test_atomic_promote_write_failure_leaves_active_unchanged(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    st.report_lesson(SCOPE, "known-good")
    active_before = st._load(SCOPE).to_json()

    def fail_promote(scope, candidate):
        raise OSError("atomic replace unavailable")

    monkeypatch.setattr(st, "_save", fail_promote)
    result = st.adapt(object(), scope=SCOPE)

    assert result["skipped"] is True
    assert not result.get("promoted")
    assert st._load(SCOPE).to_json() == active_before
    assert not any("new-strategy" in lesson for lesson in st.get_lessons(SCOPE))
