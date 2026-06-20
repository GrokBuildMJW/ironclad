"""STATE-Layout Unit D: end-to-end integration through the REAL entry points.

Drives the actual operator/​model surface — `_dispatch` for the `/vorhaben` commands and the
deterministic macros (`_stage_handover` / `_advance_pipeline`) the orchestrator triggers — in a clean
project directory, and asserts the whole-system invariant: every artifact under the active vorhaben,
engine machinery hidden under `.ironclad/`, and **the project root stays clean** (the DoD). This is
the model-free half of the D2 E2E; the model turns (`/chat`) are exercised on deploy.
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

import gx10  # noqa: E402


class _FakeAgent:
    """Stands in for the orchestrator for the deterministic slash-commands (no model turn)."""
    ran = None
    def run(self, text): self.ran = text
    def save_session(self): pass
    def status(self): return "ok"


def test_e2e_full_lifecycle_keeps_project_root_clean(tmp_path, monkeypatch):
    gx10._apply_config(gx10._code_defaults())   # boot: globals at their shipped defaults
    gx10.STORE = None                            # fresh TaskStore singleton
    monkeypatch.chdir(tmp_path)
    fake = _FakeAgent()

    # 1. fail-closed before any vorhaben — an artifact macro refuses, writes nothing
    out = gx10._stage_handover("KGC-1", "OPUS", "## Handover\nx")
    assert out.startswith("ERROR")
    assert not (tmp_path / ".work").exists() and not (tmp_path / "vault").exists()

    # 2. operator creates a software vorhaben through the real dispatch
    gx10._dispatch(fake, "vorhaben new Order Service --typ software")
    assert fake.ran is None                       # handled as a command, not a model turn
    assert gx10.active_slug() == "order-service"

    # 3. the orchestrator stages a task+handover (macro), feedback arrives, pipeline advances
    out = gx10._stage_handover(
        None, "OPUS", "## Handover\nbuild it",
        task_json='{"type":"feature","priority":"high","title":"Build X","description":"do it"}',
        force=True)
    assert out.startswith("OK")
    tid = gx10._store().list("pending")[0]["id"]
    (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text("## Result\ndone", encoding="utf-8")
    out = gx10._advance_pipeline(tid, "OPUS")
    assert out.startswith("OK")

    # 4. reconcile via the real command (MPR run routing/fail-closed is covered by the mpr suite)
    gx10._dispatch(fake, "vorhaben reconcile")

    base = tmp_path / "vault" / "order-service"
    # artifacts live under the active vorhaben
    assert (base / "tasks" / "done" / f"{tid}.json").is_file()
    assert (base / ".work" / "active.md").is_file()
    assert (base / ".work" / "archive" / "feedback" / f"{tid}_OPUS-feedback.md").is_file()
    assert (base / "INDEX.md").is_file()
    # engine machinery is hidden under .ironclad/
    assert (tmp_path / ".ironclad" / "active").read_text(encoding="utf-8").strip() == "order-service"

    # THE INVARIANT: the project root holds ONLY the two roots — nothing scattered
    roots = {p.name for p in tmp_path.iterdir()}
    assert roots == {".ironclad", "vault"}, f"project root not clean: {sorted(roots)}"
