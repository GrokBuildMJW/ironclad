"""deploy/spark static consistency lint (#216, ADR-0007), offline.

Pure logic only (the SSH verification lives in deploy/spark/verify-deployment.sh, operator-run). Lives
in `scripts/ci/` (private) → skips in an installed/clean-room tree.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CDC = _REPO / "scripts" / "ci" / "check_deploy_consistency.py"

pytestmark = pytest.mark.skipif(
    not _CDC.is_file(),
    reason="private CI check_deploy_consistency.py absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_cdc", _CDC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_valid_setup_types_parsed_from_engine():
    cdc = _load()
    assert cdc.valid_setup_types('_VALID_SETUP_TYPES = ("server", "local")') == {"server", "local"}
    assert cdc.valid_setup_types("no tuple here") == set()


def test_referenced_setup_types_picks_literals_not_expansions():
    cdc = _load()
    scripts = {
        "a.ps1": '$env:GX10_SETUP_TYPE = "local"\n',
        "b.sh": 'GX10_SETUP_TYPE=server bash x\n',
        "c.sh": 'GX10_SETUP_TYPE="${GX10_SETUP_TYPE:-}"\n',     # expansion, not a literal -> skipped
        "d.sh": '# install-type desktop -> setup.type=local\n',
    }
    refs = cdc.referenced_setup_types(scripts)
    vals = sorted(v for _, v in refs)
    assert "local" in vals and "server" in vals
    assert all(v not in ("", "GX10_SETUP_TYPE") for _, v in refs)   # no expansion captured


def test_bad_setup_types_flags_unknown_value():
    cdc = _load()
    refs = [("stop.sh", "desktop"), ("ok.sh", "local")]
    bad = cdc.bad_setup_types(refs, {"server", "local"})
    assert len(bad) == 1 and "desktop" in bad[0] and "stop.sh" in bad[0]


def test_dangling_script_refs():
    cdc = _load()
    scripts = {"release.ps1": "bash deploy/spark/deploy-mpr.sh\nbash deploy/spark/gone.sh\n"}
    existing = {"deploy/spark/deploy-mpr.sh", "deploy/spark/release.ps1"}
    v = cdc.dangling_script_refs(scripts, existing)
    assert len(v) == 1 and "deploy/spark/gone.sh" in v[0]


def test_dangling_repo_path_refs_flags_a_moved_path():
    cdc = _load()
    scripts = {"deploy/spark/sync.sh": 'tar czf x "$REPO_ROOT/skills/mpr"\nls "$REPO_ROOT/core/engine"\n'}
    refs = cdc.repo_path_refs(scripts)
    assert ("deploy/spark/sync.sh", "skills/mpr") in refs
    assert ("deploy/spark/sync.sh", "core/engine") in refs
    v = cdc.dangling_repo_path_refs(refs, {"core/engine"})              # only core/engine exists
    assert len(v) == 1 and "skills/mpr" in v[0]                         # the moved path is flagged


def test_live_deploy_tree_is_consistent():
    cdc = _load()
    scripts = cdc._scripts()
    assert scripts, "expected deploy scripts to exist"
    valid = cdc.valid_setup_types(cdc.GX10.read_text(encoding="utf-8"))
    existing = {p.relative_to(cdc.REPO_ROOT).as_posix()
                for p in cdc.DEPLOY.rglob("*") if p.suffix in (".sh", ".ps1")}
    assert cdc.bad_setup_types(cdc.referenced_setup_types(scripts), valid) == []
    assert cdc.dangling_script_refs(scripts, existing) == []
    refs = cdc.repo_path_refs(scripts)                                  # F-K-01: reference-drag guard
    real = {rel for _, rel in refs if (cdc.REPO_ROOT / rel).exists()}
    assert cdc.dangling_repo_path_refs(refs, real) == []               # live tree has no stale $REPO_ROOT refs


def test_private_spark_configs_preserve_pre_f8_effective_behavior(monkeypatch):
    sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))
    engine = _REPO / "core" / "engine"
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    import config_schema
    import gx10
    from providers import load_code_agents
    from security import SecurityPolicy

    def merged(name: str):
        raw = json.loads((_REPO / "conf" / name).read_text(encoding="utf-8"))
        projection = gx10._deep_merge(config_schema.defaults_tree(), raw)
        config_schema.validate(projection)
        return projection

    monkeypatch.delenv("GX10_PROFILE", raising=False)
    monkeypatch.delenv("GX10_SERVER_TOKEN", raising=False)
    server_cfg = merged("server.json")
    assert server_cfg["server"]["host"] == "0.0.0.0"
    assert server_cfg["security"]["allow_unauthenticated_bind"] is True
    assert server_cfg["search"]["enabled"] is True
    assert server_cfg["forge"]["enabled"] is True
    assert server_cfg["connection"]["connect_timeout_s"] == 10
    assert server_cfg["connection"]["first_token_timeout_s"] == 600
    assert SecurityPolicy.from_config(server_cfg).startup_error("0.0.0.0") is None
    server_agents = load_code_agents(server_cfg)
    for agent_id in ("OPUS", "SONNET"):
        spec = server_agents.resolve(agent_id)
        assert spec.permission_mode == "bypassPermissions"
        assert spec.capabilities.permission_bypass is True
    dockerfile = (_REPO / "core" / "Dockerfile").read_text(encoding="utf-8")
    assert "server.py --host 0.0.0.0" not in dockerfile
    bootstrap = (_REPO / "core" / "scripts" / "spark-bootstrap.sh").read_text(encoding="utf-8")
    assert 'ORCH_HOST="${GX10_SERVER_HOST:-127.0.0.1}"' in bootstrap

    local_cfg = merged("local.json")
    assert local_cfg["server"]["host"] == "127.0.0.1"
    assert local_cfg["search"]["enabled"] is True
    assert local_cfg["forge"]["enabled"] is True
    assert local_cfg["providers"]["cli_timeout_s"] == 3600
    agents = load_code_agents(local_cfg)
    for agent_id in ("OPUS", "SONNET"):
        spec = agents.resolve(agent_id)
        assert spec.permission_mode == "bypassPermissions"
        assert spec.capabilities.permission_bypass is True
