"""ACE-DEVBULLET (#855 / #880, M4-3): per-unit used-bullet correlation for the dev-process (DP-2). The
handover injection site (#863, where the coder's handover gets the playbook) DURABLY records which bullets it
injected, keyed by the task id + the issue#s the handover references (the standard `Closes #N` linkage); the
M4-2 ledger scan reads that back by the unit (issue#) to populate Trajectory.used_bullet_ids (E-004).
Covers the handover+feedback coder-addressing mode (GitHub-only autopilot + file-based-with-handovers).
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

import project_context as pc
from project_context import ProjectContext
import project_registry
import gx10
from ack import hooks
from ack import lessons as L
from playbook_store import PlaybookStore, read_unit_bullets


class _FakeAgent:
    def run(self, t): pass
    def save_session(self): pass
    def status(self): return "ok"


class _RecWorker:
    def __init__(self): self.items = []
    def submit(self, item): self.items.append(item); return True


def _leg(unit, src, dst, guard, passed, reasons=None):
    return {"unit": unit, "src": src, "dst": dst, "guard": guard, "passed": passed, "reasons": reasons or []}


def _hard_reset():
    if gx10._ACE_WORKER is not None:
        try:
            gx10._ACE_WORKER.stop()
        except Exception:
            pass
    gx10._ACE_WORKER = None
    gx10._ACE_STORE = None
    gx10._ACE_MIGRATED = False
    gx10._ACE_INJECTED.clear()
    hooks.clear_hooks()
    L.set_provider(None)


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    _hard_reset()
    saved = gx10._EFFECTIVE_CFG
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    yield
    _hard_reset()
    gx10._EFFECTIVE_CFG = saved


# ─── the unit-key derivation (tid + issue# linkage) ──────────────────────────────────────────────────
def test_unit_keys_from_tid_title_and_closes():
    keys = gx10._ace_unit_keys("KGC-7", {"title": "fix(#503): wire the gate"},
                               "## Handover\nbuild it\n\nCloses #880")
    assert keys[0] == "KGC-7"           # the engine task id first
    assert "503" in keys and "880" in keys   # the title #N + the Closes #N linkage → the dev-loop unit ids
    assert gx10._ace_unit_keys("", {}, "") == []          # nothing → empty, never raises


def test_unit_keys_dedup_and_garbage_safe():
    keys = gx10._ace_unit_keys("T", {"title": "(#5) thing"}, "Closes #5\nFixes #5")
    assert keys == ["T", "5"]           # de-duped, order-preserving
    assert gx10._ace_unit_keys("T", None, None) == ["T"]  # non-dict fields / None body → just the tid


def test_unit_keys_title_uses_only_the_first_hashnum():
    # C2 #906: only the FIRST title `#N` is the primary unit — a title that also mentions other issues must
    # NOT cross-attribute the injected bullets to them.
    keys = gx10._ace_unit_keys("T", {"title": "fix(#5): relates to #6 and #7"}, "")
    assert "5" in keys and "6" not in keys and "7" not in keys
    # a `Closes #N` in the body remains a deliberate linkage
    keys2 = gx10._ace_unit_keys("T", {"title": "fix(#5): x"}, "Closes #9")
    assert "5" in keys2 and "9" in keys2


# ─── persist (at the handover site) → read (at the M4-2 scan) ────────────────────────────────────────
def test_persist_injected_then_m42_populates_used_bullets(tmp_path, monkeypatch):
    # simulate the handover-site persist: unit #880's handover injected b-0, b-2
    gx10._ace_persist_injected(["KGC-1", "880"], ["b-0", "b-2"])
    assert read_unit_bullets(tmp_path, "880") == ["b-0", "b-2"]   # durable, keyed by the issue#
    # now the M4-2 ledger scan for a terminal unit #880 must populate used_bullet_ids from that map
    gx10._ACE_STORE = PlaybookStore(tmp_path / "pb")
    rec = gx10._ACE_WORKER = _RecWorker()
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    payloads = [_leg(880, "IMPLEMENT", "GATE", "gate", True), _leg(880, "REVIEW", "MERGE", "merge-go", True)]
    assert gx10._ace_scan_dev_ledger(payloads, []) == 1
    traj = rec.items[0]["trajectory"]
    assert traj.query == "880" and traj.outcome == "reached-human-merge-gate"
    assert traj.used_bullet_ids == ["b-0", "b-2"]         # E-004: the unit's injected bullets, correlated


def test_m42_used_bullets_empty_when_no_correlation(tmp_path, monkeypatch):
    gx10._ACE_STORE = PlaybookStore(tmp_path / "pb")
    rec = gx10._ACE_WORKER = _RecWorker()
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    gx10._ace_scan_dev_ledger([_leg(900, "REVIEW", "MERGE", "merge-go", True)], [])
    assert rec.items[0]["trajectory"].used_bullet_ids == []   # no map → [] (weaker but not wrong)


# ─── DP-2 proof: the handover the coder reads carries the injected playbook + records the correlation ──
def test_handover_carries_playbook_and_records_unit_correlation(tmp_path, monkeypatch):
    gx10._apply_config(gx10._code_defaults())
    gx10._ACE_STORE.report_lesson("ns", "always run the boundary check before the gate")
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
        gx10._stage_handover(None, "OPUS", "## Handover\nbuild it\n\nCloses #880",
                             task_json='{"type":"feature","priority":"high","title":"wire (#880)","description":"do it"}',
                             force=True)
        tid = gx10._store().list("pending")[0]["id"]
        ho = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
    # DP-2: the handover the coder reads carries the injected playbook (the #863 context_for block)
    assert "## Lessons" in ho and "always run the boundary check before the gate" in ho
    # and the injected bullets are durably recorded against the unit (#880) for the M4-2 correlation
    assert read_unit_bullets(tmp_path, "880") != []
