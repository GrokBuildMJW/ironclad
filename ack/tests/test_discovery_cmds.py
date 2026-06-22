"""Discovery commands `/prompts` + `/skills` (#147), model-free.

Exercises the engine-side discovery surface end to end: `_catalogue_snapshot` reads the **one
loaded registry** (`_PROMPTS` / `_PLAYBOOKS` / `_PLUGIN_TOOLS`), the `_render_prompts` /
`_render_skills` formatters list every loaded item, and the `_dispatch` router routes the two
commands. No model, no network — pure dispatch. Also asserts the snapshot reflects the **real**
shipped built-ins (the curated prompts + the MPR skill).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402

from ack import skillgen  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    yield
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()
    gx10._PROMPTS.clear()


def _prompt(root: Path, cap: str) -> None:
    d = root / cap
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\ncapability: {cap}\nkind: prompt\ndescription: demo {cap}\n"
        f"languages: [en, de]\nvariables: [topic]\nrequired: [topic]\n---\n"
        f"Write about {{topic}}.\n", encoding="utf-8")


def _load_mixed(tmp_path: Path, monkeypatch) -> None:
    """A built-in dir holding one of each: typed tool, playbook, prompt."""
    builtin = tmp_path / "builtin"
    skillgen.write_scaffold(skillgen.SkillSpec(capability="demo-tool", description="a demo tool",
                                               kind="tool", params=[("x", "str")]), builtin, force=True)
    skillgen.write_scaffold(skillgen.SkillSpec(capability="demo-pb", description="a demo playbook",
                                               kind="playbook", trigger=["go"]), builtin, force=True)
    _prompt(builtin, "demo-prompt")
    monkeypatch.setattr(gx10, "_BUILTIN_DIR", builtin)
    gx10._load_skills(None)


def test_snapshot_groups_prompts_and_skills(tmp_path, monkeypatch):
    _load_mixed(tmp_path, monkeypatch)
    snap = gx10._catalogue_snapshot()
    pnames = {p["name"] for p in snap["prompts"]}
    snames = {s["name"] for s in snap["skills"]}
    assert "demo-prompt" in pnames
    assert {"demo-tool", "demo-pb"} <= snames
    # prompts carry languages; skills carry a kind (playbook vs tool)
    assert next(p for p in snap["prompts"] if p["name"] == "demo-prompt")["languages"] == ["en", "de"]
    kinds = {s["name"]: s["kind"] for s in snap["skills"]}
    assert kinds["demo-pb"] == "playbook" and kinds["demo-tool"] == "tool"


def test_snapshot_is_json_serialisable(tmp_path, monkeypatch):
    # the same snapshot backs the /catalogue endpoint (#149) — must be plain JSON
    _load_mixed(tmp_path, monkeypatch)
    json.dumps(gx10._catalogue_snapshot())


def test_render_prompts_and_skills_list_loaded(tmp_path, monkeypatch):
    _load_mixed(tmp_path, monkeypatch)
    out_p = gx10._render_prompts()
    assert "demo-prompt" in out_p and "en,de" in out_p
    out_s = gx10._render_skills()
    assert "demo-tool" in out_s and "demo-pb" in out_s


def test_render_is_empty_safe():
    assert "No prompt items" in gx10._render_prompts()
    assert "No skills" in gx10._render_skills()


def test_dispatch_routes_prompts_and_skills(tmp_path, monkeypatch):
    _load_mixed(tmp_path, monkeypatch)
    captured: list[str] = []
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: captured.append(" ".join(str(x) for x in a)))
    gx10._dispatch(object(), "prompts")      # the prompts/skills branches never touch the agent
    gx10._dispatch(object(), "skills")
    blob = "\n".join(captured)
    assert "demo-prompt" in blob and "demo-tool" in blob


def test_snapshot_reflects_real_builtins():
    # integration: discovery must mirror what actually ships, with no re-scan/parallel mechanism
    gx10._load_skills(None)
    snap = gx10._catalogue_snapshot()
    pnames = {p["name"] for p in snap["prompts"]}
    snames = {s["name"] for s in snap["skills"]}
    assert {"code-review", "commit-message", "bug-report", "explain-code"} <= pnames
    assert "mpr_research" in snames           # the MPR built-in is a typed-tool skill
