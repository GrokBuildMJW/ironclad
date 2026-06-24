"""clean-room PRE-publish proof runner (#348 S6), offline.

Pins the pure step PLAN + the `--dry-run` / fail-closed CLI paths (the heavy build/venv/install is network
and runs only on a real DELIVER, so it is NOT exercised here). Lives in `scripts/ci/` (private) -> skips in
an installed/clean-room tree.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CR = _REPO / "scripts" / "ci" / "clean_room.py"

pytestmark = pytest.mark.skipif(
    not _CR.is_file(),
    reason="private CI clean_room.py absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_cleanroom", _CR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_clean_room_plan_is_build_venv_install_smoke_noneditable():
    cr = _load()
    plan = cr.clean_room_plan("/staged", "/venv", py="PY")
    assert [n for n, _ in plan] == ["build-wheel", "make-venv", "upgrade-pip", "install-wheel", "import-smoke"]
    install = dict(plan)["install-wheel"]
    assert "-e" not in install and "--find-links" in install and "ironclad-ai" in install   # NON-editable
    assert dict(plan)["build-wheel"][:4] == ["PY", "-m", "build", "--wheel"]                 # wheel from staged
    assert "import ack" in dict(plan)["import-smoke"][-1]                                     # installed-pkg smoke


def test_clean_room_venv_python_path_is_platform_correct():
    cr = _load()
    vpy = cr._venv_python(Path("/v"))
    assert vpy.endswith("python") and ("Scripts" in vpy or "bin" in vpy)


def test_clean_room_dry_run_prints_plan_runs_nothing(tmp_path):
    cr = _load()
    (tmp_path / "staged").mkdir()
    assert cr.main(["--staged", str(tmp_path / "staged"), "--dry-run"]) == 0   # nothing built


def test_clean_room_missing_staged_is_fail_closed(tmp_path):
    cr = _load()
    assert cr.main(["--staged", str(tmp_path / "nope")]) == 1                  # absent staged dir => RED


def test_clean_room_mirrors_public_workflow_import_contract():
    # fork-5 constraint: the public clean-room.yml can't import this private runner, so they are parallel.
    # Pin the shared contract — both build a wheel, use a fresh venv, and import the SAME modules.
    cr = _load()
    wf = (_REPO / "core" / ".github" / "workflows" / "clean-room.yml").read_text(encoding="utf-8")
    for mod in ("import ack", "ack.lodestar", "ack.sdk"):
        assert mod in cr.IMPORT_SMOKE and mod in wf
    flat = " ".join(c for _, cmds in cr.clean_room_plan("/s", "/v") for c in cmds)
    assert "venv" in flat and "venv" in wf and "build" in flat and "--wheel" in flat
