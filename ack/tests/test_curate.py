"""Pure curated-global helpers (#634 / ADR-0011 AD-9) — offline, like test_scope_guard / test_reflect_policy."""
from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the pure module DIRECTLY by path — do NOT put memory-service/ on sys.path (its app.py would shadow
# the engine's `app` module for the rest of the pytest session, and connects to Mem0 at import).
_C_PATH = Path(__file__).resolve().parents[2] / "memory-service" / "curate.py"
_spec = importlib.util.spec_from_file_location("curate", _C_PATH)
curate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(curate)


def test_promote_gate_is_fail_closed():
    # unconfirmed → refused (promotion is never the normal /add path)
    assert "confirm" in (curate.promote_refusal(confirm=False, from_agent_id="ns", memory="x", query=None) or "")
    # missing source scope → refused
    assert "from_agent_id" in (curate.promote_refusal(confirm=True, from_agent_id="", memory="x", query=None) or "")
    # both payloads → refused; neither → refused
    assert "not both" in (curate.promote_refusal(confirm=True, from_agent_id="ns", memory="x", query="q") or "")
    assert curate.promote_refusal(confirm=True, from_agent_id="ns", memory=None, query=None) is not None
    # valid: an operator-redacted memory text, OR a source query to copy matches from
    assert curate.promote_refusal(confirm=True, from_agent_id="ns", memory="redacted", query=None) is None
    assert curate.promote_refusal(confirm=True, from_agent_id="ns", memory=None, query="topic") is None


def test_merge_project_wins_precedence_dedup_and_cap():
    project = {"results": [{"memory": "P1"}, {"memory": "P2"}]}
    curated = {"results": [{"memory": "C1"}, {"memory": "P1"}, {"memory": "C2"}]}  # the P1 dup must be dropped
    out = curate.merge_project_wins(project, curated, limit=4)
    texts = [r.get("memory") for r in out["results"]]
    assert texts[:2] == ["P1", "P2"]                       # project first, order + position preserved
    assert texts.count("P1") == 1 and "C1" in texts and "C2" in texts   # curated fills; project-wins dedup
    assert len(out["results"]) == 4                        # capped at limit
    # provenance: appended curated hits are tagged; project hits are not
    assert all(r.get("curated") for r in out["results"] if r.get("memory") in ("C1", "C2"))
    assert not any(r.get("curated") for r in out["results"] if r.get("memory") in ("P1", "P2"))


def test_merge_project_wins_degrades_on_odd_shape():
    assert curate.merge_project_wins({"results": [{"memory": "P"}]}, {}, 5)["results"] == [{"memory": "P"}]
    assert curate.merge_project_wins({}, {"results": [{"memory": "C"}]}, 0)["results"] == []   # cap 0 → empty
    assert curate.merge_project_wins({}, {}, 5)["results"] == []                               # both empty


def test_curated_agent_id_is_a_distinct_fixed_partition():
    # the curated tier lives under its own fixed partition in its OWN collection — never a project mem_ns
    assert isinstance(curate.CURATED_AGENT_ID, str) and curate.CURATED_AGENT_ID
