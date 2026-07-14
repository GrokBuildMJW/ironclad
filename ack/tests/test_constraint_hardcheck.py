"""Kept build anti-drift check against approved design metadata."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from ack.ace.constraint_conflict import Violation, hardcheck

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import ack.ace.constraint_conflict as conflict  # noqa: E402


def _setup(monkeypatch, tmp_path):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "FRAMING_NOTES_ENABLED", False)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _impl_json(title="build it", **typed):
    payload = {"type": "implementation", "priority": "high", "title": f"Implement approved {title}",
               "description": "Implement the approved design with complete validation and regression coverage."}
    payload.update(typed)
    return json.dumps(payload)


@pytest.mark.parametrize(
    ("required", "provided", "kind"),
    [
        ({"language": "python"}, {}, "missing"),
        ({"language": "python"}, {"language": "rust"}, "mismatch"),
    ],
)
def test_hardcheck_reports_first_violation(required, provided, kind):
    v = hardcheck(required, provided, require_present=True)
    assert isinstance(v, Violation)
    assert v.kind == kind
    assert v.category == "language"


def test_hardcheck_allows_match_and_empty_floor():
    assert hardcheck({"language": "python"}, {"language": "python"}, require_present=True) is None
    assert hardcheck({}, {"language": "rust"}, require_present=True) is None


def test_design_build_check_refuses_task_drift(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Python", language="python")
    assert gx10._approve_design().startswith("OK")

    out = gx10._stage_handover(None, "OPUS", "body", _impl_json(language="rust"))

    assert out.startswith("ERROR")
    assert "approved design requires language='python'" in out
    assert gx10._store().list("pending") == []


def test_design_build_check_requires_present_task_field(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Python", language="python")
    assert gx10._approve_design().startswith("OK")

    out = gx10._stage_handover(None, "OPUS", "body", _impl_json())

    assert out.startswith("ERROR")
    assert "task typed field is missing" in out


def test_design_build_check_allows_matching_task(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Python", language="python")
    assert gx10._approve_design().startswith("OK")

    out = gx10._stage_handover(None, "OPUS", "body", _impl_json(language="python"))

    assert out.startswith("OK")
    assert len(gx10._store().list("pending")) == 1


def test_empty_approved_design_metadata_is_noop(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "plain design")
    assert gx10._approve_design().startswith("OK")

    out = gx10._stage_handover(None, "OPUS", "body", _impl_json(language="rust"))

    assert out.startswith("OK")


def test_b1_network_separation_is_permanent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design(
        "Approach",
        "Use Python.\n\n## Build policy\n\nnetwork: none\n",
        language="python",
        network="true",
    )
    proposal = gx10.vault_root() / gx10.active_slug() / "proposals" / "design-1.md"
    assert "\nnetwork:" not in proposal.read_text(encoding="utf-8").split("---", 2)[1]
    assert gx10._approve_design().startswith("OK")
    decision = gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md"
    decision_text = decision.read_text(encoding="utf-8")
    assert "\nnetwork:" not in decision_text.split("---", 2)[1]
    assert "## Build policy\n\nnetwork: none" in decision_text
    assert gx10._design_typed(gx10.active_slug()) == {"language": "python"}
    assert gx10._design_egress_policy(gx10.active_slug())["network"] == "none"

    seen = []
    real_hardcheck = conflict.hardcheck

    def capture(required, provided, **kwargs):
        seen.append((dict(required), dict(provided)))
        return real_hardcheck(required, provided, **kwargs)

    monkeypatch.setattr(conflict, "hardcheck", capture)
    assert gx10._design_build_check(
        gx10.active_slug(), {"language": "python", "network": False}
    ) is None
    assert gx10._design_build_check(
        gx10.active_slug(), {"language": "rust", "network": False}
    ) is not None
    assert seen == [
        ({"language": "python"}, {"language": "python", "network": False}),
        ({"language": "python"}, {"language": "rust", "network": False}),
    ]
