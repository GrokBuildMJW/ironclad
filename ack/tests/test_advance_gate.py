"""#1463 — always-on, fail-closed completion authority."""
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


def _setup(monkeypatch, tmp_path):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _staged(content: str | None = "---\nstatus: done\n---\nDone\n", *, agent="OPUS"):
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)["id"]
    gx10._store().transition(tid, "in_progress")
    if content is not None:
        fb = gx10.feedback_dir() / f"{tid}_{agent}-feedback.md"
        fb.parent.mkdir(parents=True, exist_ok=True)
        fb.write_text(content, encoding="utf-8")
    return tid


def _assert_refused(tid: str, out: str) -> None:
    assert out.startswith("ERROR: not advancing") or out.startswith("ERROR: feedback missing")
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_advance_allows_only_explicit_done(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged()

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"


def test_advance_finds_done_feedback_regardless_of_caller_agent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged(agent="SONNET")

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"


@pytest.mark.parametrize(
    ("content", "token"),
    [
        ("---\nstatus: blocked\n---\n", "blocked"),
        ("---\nstatus: clarification_needed\n---\n", "clarification_needed"),
        ("feedback without a status\n", "missing"),
        ("---\nstatus done\n---\n", "missing"),
        ("---\nstatus: finished\n---\n", "finished"),
        ("status: done-ish\n", "done-ish"),
        ("\n".join(["prose"] * 20 + ["status: done"]), "missing"),
    ],
    ids=["blocked", "clarification", "missing", "malformed-frontmatter", "unknown", "unknown-prefix", "misplaced"],
)
def test_advance_refuses_every_non_done_status(monkeypatch, tmp_path, content, token):
    _setup(monkeypatch, tmp_path)
    tid = _staged(content)
    monkeypatch.setattr(gx10, "_egress_advance_check_log",
                        lambda: (_ for _ in ()).throw(AssertionError("egress must follow completion authority")))

    out = gx10._advance_pipeline(tid, "OPUS")

    _assert_refused(tid, out)
    assert token in out


def test_advance_refuses_missing_feedback(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged(None)

    out = gx10._advance_pipeline(tid, "OPUS")

    _assert_refused(tid, out)


def test_advance_refuses_empty_feedback(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged("\n\t")

    out = gx10._advance_pipeline(tid, "OPUS")

    _assert_refused(tid, out)
    assert "feedback is empty" in out


def test_advance_refuses_unreadable_feedback(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged()
    target = gx10.feedback_dir() / f"{tid}_OPUS-feedback.md"
    original = Path.read_text

    def _read_text(path, *args, **kwargs):
        if path == target:
            raise OSError("permission denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    out = gx10._advance_pipeline(tid, "OPUS")

    _assert_refused(tid, out)
    assert "feedback is unreadable" in out and "permission denied" in out


@pytest.mark.parametrize(
    "text",
    [
        "---\nstatus: done\n---\n",
        "---\nStatus: DONE (all checks green)\n---\n",
        "status: done\n---\nfrom: SONNET\n---\n",
        "STATUS: Done - complete\n\nSummary\n",
        "Completion report\nAll focused tests passed.\nStatus: DONE ready for review\n",
        "Introductory prose\nstatus: done (with trailing text)\n",
        "status: done.\n",
        "status: done,\n",
        'status: "done"\n',
    ],
    ids=[
        "frontmatter",
        "frontmatter-case-trailing",
        "bare-leading",
        "bare-leading-case-trailing",
        "bounded-prose-case-trailing",
        "bounded-prose-trailing",
        "trailing-period",
        "trailing-comma",
        "wrapping-quotes",
    ],
)
def test_feedback_status_done_spelling_matrix(text):
    assert gx10._feedback_status(text) == "done"
    assert gx10._advance_gate(text) is None


@pytest.mark.parametrize("token", ["done-ish", "donezo", "complete"])
def test_feedback_status_refuses_non_done_spellings(token):
    text = f"status: {token}\n"
    assert gx10._feedback_status(text) == token
    assert gx10._advance_gate(text) is not None


@pytest.mark.parametrize("value", [True, False], ids=["legacy-true", "legacy-false"])
@pytest.mark.parametrize(
    "content",
    ["---\nstatus: blocked\n---\n", "no status\n", "---\nstatus: finished\n---\n"],
    ids=["blocked", "missing", "unknown"],
)
def test_advance_gate_tombstone_cannot_disable_refusal(monkeypatch, tmp_path, capsys, value, content):
    cfg = gx10._code_defaults()
    cfg["advance_gate"] = {"enabled": value}
    gx10._apply_config(cfg)
    gx10._apply_config(cfg)
    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1
    assert "advance_gate.enabled" in warnings[0] and "always on" in warnings[0]
    assert "advance_gate" not in cfg
    assert not hasattr(gx10, "ADVANCE_GATE_ENABLED")

    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")
    tid = _staged(content)
    _assert_refused(tid, gx10._advance_pipeline(tid, "OPUS"))


def test_advance_gate_tombstone_loaded_from_file(tmp_path, capsys):
    source = tmp_path / "legacy.json"
    source.write_text('{"advance_gate": {"enabled": false}}', encoding="utf-8")
    cfg = gx10._deep_merge(gx10._code_defaults(), gx10._load_config_tree(source))

    gx10._apply_config(cfg)

    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1 and "advance_gate.enabled" in warnings[0]
    assert "advance_gate" not in cfg


def test_runtime_set_refuses_retired_advance_gate(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))

    gx10._dispatch(None, "config set advance_gate.enabled false")

    assert len(surfaced) == 1
    assert "retired and cannot be set" in surfaced[0]
    assert "advance_gate" not in cfg
