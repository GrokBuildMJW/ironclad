"""S10b engine seam: gx10 generate command writes into the project library."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc  # noqa: E402
from project_context import ProjectContext  # noqa: E402
import gx10  # noqa: E402
from ack.gate import gate_prompt  # noqa: E402


def test_project_library_root_resolves_under_ctx_vault(tmp_path: Path) -> None:
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        assert gx10._project_library_root() == Path(str(tmp_path)) / "vault" / "library"
    assert pc.current() is None


def test_builtin_capabilities_is_a_nonempty_set() -> None:
    caps = gx10._builtin_capabilities()
    assert isinstance(caps, set) and caps  # core/skills has at least one built-in


def test_generate_command_writes_into_project_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: set())
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        out = gx10._generate_command(
            "--domain demo --case widget --description x --prefix p"
        )
        lib = gx10._project_library_root()
        assert "project library" in out
        assert lib.exists() and any(lib.rglob("*"))
    assert pc.current() is None


def test_generate_command_refuses_builtin_collision_nothing_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: {"p-widget"})
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        out = gx10._generate_command(
            "--domain demo --case widget --description x --prefix p"
        )
        lib = gx10._project_library_root()
        assert "REFUSED" in out
        assert (not lib.exists()) or list(lib.rglob("*")) == []  # nothing written on refusal
    assert pc.current() is None


def test_generate_command_empty_args_usage() -> None:
    out = gx10._generate_command("")
    assert "usage:" in out.lower()


def test_generate_command_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: set())
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        out = gx10._generate_command(
            "--domain demo --case widget --description x --prefix p --dry-run"
        )
        lib = gx10._project_library_root()
        assert ("dry-run" in out.lower()) and (
            (not lib.exists()) or list(lib.rglob("*")) == []
        )
    assert pc.current() is None


def test_generate_command_ignores_user_output_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # the engine enforces the per-project library; a user --output-root must NOT redirect generation
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: set())
    rogue = tmp_path / "rogue"
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        gx10._generate_command(
            f"--domain demo --case widget --description x --prefix p --output-root {rogue}"
        )
        lib = gx10._project_library_root()
        assert lib.exists() and any(lib.rglob("*"))     # wrote into the project library
    assert (not rogue.exists()) or list(rogue.rglob("*")) == []   # NOT into the user-supplied root
    assert pc.current() is None


def test_generate_command_bad_template_returns_error_no_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: set())
    missing = tmp_path / "no-such-template"
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        out = gx10._generate_command(
            f"--domain demo --case widget --description x --prefix p --template {missing}"
        )
    assert out.startswith("generate:")          # clean error string, no exception escaped
    assert pc.current() is None


def test_generate_command_unbalanced_quote_returns_error() -> None:
    out = gx10._generate_command('--domain demo --description "unterminated')
    assert out.startswith("generate:")          # shlex ValueError handled, no raise


def test_project_library_root_default_fallback_is_relative() -> None:
    # no active project => the boot workdir's vault/library (relative), byte-identical to single-project
    assert pc.current() is None
    assert gx10._project_library_root() == Path("vault") / "library"


def test_generate_command_kind_prompt_writes_gate_valid_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda **kw: set())
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        out = gx10._generate_command(
            "--kind prompt --domain writing --case blog-brief --description x"
        )
        lib = gx10._project_library_root()
        skill = lib / "Writing" / "blog-brief" / "SKILL.md"
        assert "project library" in out
        assert skill.exists()
        gr = gate_prompt(skill)
        assert gr.passed, gr.reasons
    assert pc.current() is None


def test_generate_command_kind_prompt_refuses_builtin_prompt_collision(
    tmp_path: Path
) -> None:
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        out = gx10._generate_command(
            "--kind prompt --domain x --case review --prefix code --description y"
        )
        lib = gx10._project_library_root()
        assert "REFUSED" in out
        assert (not lib.exists()) or list(lib.rglob("*")) == []
    assert pc.current() is None


def test_builtin_capabilities_include_prompts_is_superset() -> None:
    base = gx10._builtin_capabilities()
    withp = gx10._builtin_capabilities(include_prompts=True)
    assert base <= withp
    assert "code-review" in withp
