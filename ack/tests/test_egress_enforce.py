"""F-B S5 (#1438): POST-coder egress enforcement on advance."""
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
import engine.egress_runner as egress_runner  # noqa: E402


def _setup(monkeypatch, tmp_path, *, policy="network: none\n"):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")
    body = "Use Python." if policy is None else f"Use Python.\n\n## Build policy\n\n{policy}"
    gx10.record_design("Approach", body)
    assert gx10._approve_design().startswith("OK")
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(egress_runner, "run_hermetic", lambda root, *, network, sandbox_pref="auto": {"findings": []})
    return _make_in_progress_task()


def _make_in_progress_task():
    task = gx10._store().create({
        "type": "implementation",
        "priority": "high",
        "title": "Build feature",
        "description": "Produce code",
    })
    gx10._store().transition(task["id"], "in_progress")
    return task["id"]


def _write_feedback(task_id: str, agent: str = "OPUS") -> None:
    gx10.feedback_dir().mkdir(parents=True, exist_ok=True)
    (gx10.feedback_dir() / f"{task_id}_{agent}-feedback.md").write_text(
        "status: done\n\n## Summary\nok\n",
        encoding="utf-8",
    )


def _advance(task_id: str) -> str:
    _write_feedback(task_id)
    return gx10._advance_pipeline(task_id, "OPUS")


def test_egress_enable_controls_are_tombstones_and_cannot_disable(monkeypatch, tmp_path, capsys):
    cfg = gx10._code_defaults()
    cfg["security"]["egress_analysis"] = {"enabled": False}
    gx10._apply_config(cfg)
    gx10._apply_config(cfg)
    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1
    assert "security.egress_analysis.enabled" in warnings[0] and "always on" in warnings[0]
    assert "egress_analysis" not in cfg["security"]
    assert not hasattr(gx10, "EGRESS_ANALYSIS_ENABLED")

    monkeypatch.setenv("GX10_EGRESS_ANALYSIS_ENABLED", "0")
    env_cfg = gx10._apply_env(gx10._code_defaults())
    assert "egress_analysis" not in env_cfg["security"]
    assert "GX10_EGRESS_ANALYSIS_ENABLED" in capsys.readouterr().out

    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", env_cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set security.egress_analysis.enabled false")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
    assert "egress_analysis" not in env_cfg["security"]

    tid = _setup(monkeypatch, tmp_path)
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")

    out = _advance(tid)

    assert out.startswith("ERROR: egress analysis refused advance")
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_network_none_known_egress_dependency_refuses_and_keeps_in_progress(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path)
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")

    out = _advance(tid)

    assert out.startswith("ERROR: egress analysis refused advance")
    assert "package requests" in out
    assert "allow: <package>" in out
    task = gx10._store().get(tid)
    assert task["status"] == "in_progress"
    assert not task.get("blocked")


def test_allow_listed_dependency_advances(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path, policy="network: none\nallow: requests\n")
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")

    out = _advance(tid)

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"


def test_network_open_skips_egress_analysis(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path, policy="network: open\n")
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    monkeypatch.setattr(gx10, "_egress_advance_findings",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))

    out = _advance(tid)

    assert out.startswith("OK: pipeline advanced")
    assert "egress advisory:" not in out
    assert "egress analysis refused" not in out
    assert "egress analysis skipped" not in out


def test_static_advisory_surfaces_but_does_not_refuse(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_egress_advance_findings",
                        lambda root, pol: ([], ["import socket (app.py:1): stdlib raw socket module import"]))

    out = _advance(tid)

    assert out.startswith("OK: pipeline advanced")
    assert "egress advisory: import socket" in out
    assert gx10._store().get(tid)["status"] == "done"


def test_analyzer_exception_refuses_under_restrictive_posture(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_egress_advance_findings",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    out = _advance(tid)

    assert out.startswith("ERROR: egress analysis refused advance")
    assert "fail-closed" in out
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_no_code_root_refuses_under_restrictive_posture(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_project_root", lambda: None)
    monkeypatch.setattr(gx10, "_exec_cwd", lambda: str(tmp_path / "missing-code-root"))

    out = _advance(tid)

    assert out.startswith("ERROR: egress analysis refused advance")
    assert "requires a code root" in out
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_approved_design_without_build_policy_advances_without_analyzers(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path, policy=None)
    monkeypatch.setattr(gx10, "_egress_advance_findings",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))

    out = _advance(tid)

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"


@pytest.mark.parametrize(
    "policy",
    ["allow: requests\n", "network: maybe\n"],
    ids=["missing-network", "invalid-network"],
)
def test_declared_build_policy_with_missing_or_invalid_network_refuses(monkeypatch, tmp_path, policy):
    tid = _setup(monkeypatch, tmp_path, policy=policy)
    monkeypatch.setattr(gx10, "_egress_advance_findings",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))

    out = _advance(tid)

    assert out.startswith("ERROR: egress analysis refused advance")
    assert "posture is missing or invalid" in out
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_design_migration_refusal_is_returned_and_keeps_in_progress(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        gx10,
        "_design_egress_policy",
        lambda _slug: (_ for _ in ()).throw(gx10.DesignMigrationRefusal("vault conflict")),
    )

    out = _advance(tid)

    assert out.startswith("ERROR: egress analysis refused advance")
    assert "approved design vault is unreadable" in out
    assert "Reconcile decisions/design.md" in out
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_only_block_severity_refuses(monkeypatch, tmp_path):
    tid = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_egress_advance_findings",
                        lambda root, pol: ([], ["package requests: known egress-capable dependency"]))

    out = _advance(tid)

    assert out.startswith("OK: pipeline advanced")
    assert "egress advisory: package requests" in out
