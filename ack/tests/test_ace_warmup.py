"""ACE-OFFLINE-WIRE (#855 wiring-audit follow-up / #915): the offline warm-start is now reachable via the
`/ace warmup --ledger <path>` command (was orphaned). PlaybookStore.warmup batch-replays past trajectories
to seed a scope's playbook; gx10._ace_command wires it, reading the ledger as plain data (boundary-clean).
Off the hot path, fail-soft, no-op without a model/scope/ledger. Deterministic (fake chat, stubbed ledger).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_registry
import gx10
from playbook_store import PlaybookStore


def _chat(insight):
    payload = json.dumps({"insights": [{"content": insight, "section": "strategies_and_hard_rules"}], "ratings": []})
    return lambda prompt: payload


def _leg(unit, src, dst, guard, passed):
    return {"unit": unit, "src": src, "dst": dst, "guard": guard, "passed": passed, "reasons": []}


def _merged(unit):
    return [_leg(unit, "IMPLEMENT", "GATE", "gate", True), _leg(unit, "REVIEW", "MERGE", "merge-go", True)]


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    gx10._ACE_STORE = None
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    yield
    gx10._ACE_STORE = None


# ─── PlaybookStore.warmup ────────────────────────────────────────────────────────────────────────────
def test_store_warmup_seeds_from_trajectories(tmp_path):
    from ack.ace import Trajectory
    store = PlaybookStore(tmp_path / "pb"); store.set_transports(chat=_chat("a warm-started lesson"))
    report = store.warmup("ns", [Trajectory(query="build the parser", outcome="success")])
    assert not report.get("skipped") and report.get("added", 0) >= 1
    assert any("warm-started lesson" in l for l in store.get_lessons("ns"))


def test_store_warmup_noop_without_a_model(tmp_path):
    from ack.ace import Trajectory
    store = PlaybookStore(tmp_path / "pb")                      # no chat transport
    report = store.warmup("ns", [Trajectory(query="q", outcome="success")])
    assert report.get("skipped") and store.get_lessons("ns") == []


# ─── the /ace warmup command ─────────────────────────────────────────────────────────────────────────
def test_ace_command_usage():
    assert "usage: /ace warmup" in gx10._ace_command("")
    assert "usage: /ace warmup" in gx10._ace_command("warmup")          # missing --ledger
    assert "usage: /ace warmup" in gx10._ace_command("frobnicate")      # unknown subcommand


def test_ace_command_warmup_flow(tmp_path, monkeypatch):
    store = gx10._ACE_STORE = PlaybookStore(tmp_path / "pb"); store.set_transports(chat=_chat("ledger-warmed lesson"))
    monkeypatch.setattr(gx10, "_read_ledger_payloads", lambda p: (_merged(100) + _merged(101), []))
    out = gx10._ace_command("warmup --ledger /some/.devloop/ledger.jsonl")
    assert "replayed" in out
    assert any("ledger-warmed lesson" in l for l in store.get_lessons("ns"))


def test_ace_command_warmup_failsoft(tmp_path, monkeypatch):
    # no store registered
    assert "no ACE playbook store" in gx10._ace_command("warmup --ledger /x")
    # a chain-tampered ledger is blocked, never seeds
    store = gx10._ACE_STORE = PlaybookStore(tmp_path / "pb"); store.set_transports(chat=_chat("x"))
    monkeypatch.setattr(gx10, "_read_ledger_payloads", lambda p: ([], ["record 2: hash mismatch"]))
    assert "BLOCKED" in gx10._ace_command("warmup --ledger /x")
    assert store.get_lessons("ns") == []


# ─── #918: the /ace eval efficiency diagnostic (evaluation now on a live path) ───────────────────────
def test_store_benchmark_reports_paper_efficiency(tmp_path):
    from ack.ace import Trajectory
    store = PlaybookStore(tmp_path / "pb"); store.set_transports(chat=_chat("x"))
    rep = store.benchmark([Trajectory(query="q1", outcome="success"), Trajectory(query="q2", outcome="success")])
    assert not rep.get("skipped")
    assert rep["ace"].full_rewrites == 0 and rep["no_full_rewrite"] is True          # J-001: ACE never full-rewrites
    assert rep["evolutionary"].rollouts > rep["ace"].rollouts                        # ACE is cheaper than evolutionary
    assert 0.0 <= rep["rollout_reduction_vs_evolutionary"] <= 1.0
    assert store.get_lessons("ns") == []                                             # pure measurement: live playbook untouched


def test_store_benchmark_noop_without_a_model(tmp_path):
    from ack.ace import Trajectory
    assert PlaybookStore(tmp_path / "pb").benchmark([Trajectory(query="q", outcome="success")]).get("skipped")


def test_ace_command_eval_flow(tmp_path, monkeypatch):
    store = gx10._ACE_STORE = PlaybookStore(tmp_path / "pb"); store.set_transports(chat=_chat("x"))
    monkeypatch.setattr(gx10, "_read_ledger_payloads", lambda p: (_merged(100) + _merged(101), []))
    out = gx10._ace_command("eval --ledger /some/.devloop/ledger.jsonl")
    assert "ace eval" in out and "no-full-rewrite (J-001): PASS" in out and "J-002 >50%" in out
    # usage covers eval too; a chain-tampered ledger blocks eval
    assert "usage: /ace warmup" in gx10._ace_command("") and "/ace eval" in gx10._ace_command("")
    monkeypatch.setattr(gx10, "_read_ledger_payloads", lambda p: ([], ["hash mismatch"]))
    assert "BLOCKED" in gx10._ace_command("eval --ledger /x")
