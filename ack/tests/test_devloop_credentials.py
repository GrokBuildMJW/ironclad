"""Driver credential contract + IMPLEMENT tool-fence (epic #262, S11 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins that the agent
env is scrubbed of every token (incl. the marker key K), that the tool-fence rejects merge/push/
release verbs while allowing benign commands, and that a mis-scoped driver token is refused at start.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CRED = _REPO / "scripts" / "devloop" / "credentials.py"

pytestmark = pytest.mark.skipif(
    not _CRED.is_file(),
    reason="private dev-loop credentials (scripts/devloop/credentials.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_credentials", _CRED)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_scrub_strips_every_token_keeps_benign():
    c = _load()
    env = {"PATH": "/usr/bin", "HOME": "/h", "GH_TOKEN": "x", "UPSTREAM_TOKEN": "y",
           "GX10_DEVLOOP_MARKER_KEY": "k", "MY_API_SECRET": "s", "SOME_PASSWORD": "p"}
    scrubbed = c.scrub_agent_env(env)
    assert set(scrubbed) == {"PATH", "HOME"}
    assert c.leaked_secrets(scrubbed) == []
    assert c.leaked_secrets(env)                                  # the original leaks


def test_tool_fence_rejects_mutating_verbs_allows_benign():
    c = _load()
    assert c.tool_fence_violations("git push origin main") == ["git push"]
    assert "gh pr merge" in c.tool_fence_violations("gh pr merge 5 --squash")
    assert c.tool_fence_violations("git status && pytest -q") == []
    assert c.tool_fence_violations("gh issue view 1") == []


def test_refuse_to_start_on_an_over_scoped_token():
    c = _load()
    allowed = ["owner/target-repo"]                              # generic; no private repo literal
    assert c.refuse_to_start(allowed, allowed) == []
    over = c.refuse_to_start(["owner/target-repo", "owner/public-repo", "pypi"], allowed)
    assert len(over) == 2 and any("public-repo" in x for x in over)


# ── #348 S2 credential-lane hardening ──
def test_go_secret_is_scrubbed_explicitly():
    # the supervised-gate GO secret must never reach the agent — added to the explicit allow-list, not
    # merely caught by the regex (so a rename of the env var cannot silently un-scrub it).
    c = _load()
    assert "GX10_DEVLOOP_GO_SECRET" in c.SECRET_ENV_VARS
    env = {"GX10_DEVLOOP_GO_SECRET": "g", "PATH": "/usr/bin"}
    scrubbed = c.scrub_agent_env(env)
    assert "GX10_DEVLOOP_GO_SECRET" not in scrubbed
    assert c.leaked_secrets(scrubbed) == []


def test_fence_catches_publish_triggers_path_and_bare():
    c = _load()
    assert c.tool_fence_violations("gh workflow run publish.yml") == ["gh workflow run"]
    assert "gh run rerun" in c.tool_fence_violations("gh run rerun 12345")
    assert "twine" in c.tool_fence_violations("twine upload dist/*")
    assert "twine" in c.tool_fence_violations("python -m twine upload dist/*")
    assert "publish_core.sh" in c.tool_fence_violations("bash scripts/ci/publish_core.sh --push")  # path
    assert "publish_core.sh" in c.tool_fence_violations("publish_core.sh")                          # bare
    # negative: a benign build/inspect command carrying none of the verbs still passes
    assert c.tool_fence_violations("python -m pytest -q && gh issue view 1") == []


def test_fence_catches_ecosystem_publishers_and_gh_api_method():
    # the docstring claim "every direct publish trigger we can name" must cover the PyPI/npm publishers and
    # the long-form `gh api --method` (publish.yml fires OIDC on workflow_dispatch via the REST API too).
    c = _load()
    for cmd, verb in [("npm publish --access public", "npm publish"),
                      ("yarn publish", "yarn publish"),
                      ("pnpm publish", "pnpm publish"),
                      ("poetry publish --build", "poetry publish"),
                      ("flit publish", "flit publish"),
                      ("hatch publish", "hatch publish"),
                      ("python setup.py upload", "python setup.py upload"),
                      ("gh api --method POST repos/o/r/dispatches", "gh api --method")]:
        assert verb in c.tool_fence_violations(cmd), cmd
    # negative: read-only / benign neighbours of those verbs still pass
    assert c.tool_fence_violations("npm ci && npm test") == []
    assert c.tool_fence_violations("gh api repos/o/r/issues") == []


def test_harden_agent_env_redirects_credential_paths_keeps_home(tmp_path):
    c = _load()
    env = {"PATH": "/usr/bin", "HOME": "/real/home", "GH_TOKEN": "t",
           "GX10_DEVLOOP_GO_SECRET": "g", "GX10_DEVLOOP_MARKER_KEY": "k"}
    h = c.harden_agent_env(env, tmp_path / "scratch")
    # no declared secret env var survives (referenced via the module constant — no literal names here)
    assert not (set(c.SECRET_ENV_VARS) & set(h))
    # HOME deliberately preserved — the agent's own ~/.claude model credential is HOME-based
    assert h["HOME"] == "/real/home"
    # git/gh credential discovery redirected into the scratch dir
    assert h["GIT_CONFIG_NOSYSTEM"] == "1" and h["GIT_TERMINAL_PROMPT"] == "0"
    # GCM store = a cross-platform EMPTY file store (not `cache`, which errors on Windows) + no prompt
    assert h["GCM_INTERACTIVE"] == "never" and h["GCM_CREDENTIAL_STORE"] == "plaintext"
    assert Path(h["GCM_PLAINTEXT_STORE_PATH"]).is_dir()                  # empty -> a helper reads nothing
    global_cfg = Path(h["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
    assert "[credential]" in global_cfg and "helper =" in global_cfg     # helper list reset to empty
    assert Path(h["GH_CONFIG_DIR"]).is_dir()
    # the leak audit must ignore the deliberately-set selectors (GCM_CREDENTIAL_STORE matches CREDENTIAL)
    assert "GCM_CREDENTIAL_STORE" in c.leaked_secrets(h)                 # flagged without the exemption
    assert c.leaked_secrets(h, ignore=c.HARDENING_KEYS) == []            # clean with it


def test_leaked_secrets_ignore_does_not_mask_a_real_secret():
    c = _load()
    env = {"GCM_CREDENTIAL_STORE": "cache", "GH_TOKEN": "t"}             # one selector + one real token
    assert c.leaked_secrets(env, ignore=c.HARDENING_KEYS) == ["GH_TOKEN"]


def test_hardened_env_denies_the_default_credential_path(tmp_path):
    """Containment of the DEFAULT credential-resolution path: under the hardened env `git credential fill`
    for github.com resolves NO password (no helper configured, no interactive prompt). Meaningful on the
    real GCM-backed Windows delivery host (a clean-Linux green is weaker — no helper, no prompt — but valid).

    NOT a claim of absolute containment: a deliberately-adversarial same-user agent can re-add a helper AND
    re-select the OS store (`GCM_CREDENTIAL_STORE=wincredman`) to read the user-keyed DPAPI vault — that
    requires process/identity isolation (Phase-3) or the least-privilege delivery credential (S7), per the
    credentials.py module "containment ceiling" note. This test pins the env layer, not the ceiling."""
    c = _load()
    h = c.harden_agent_env(os.environ, tmp_path / "scratch")
    p = subprocess.run(["git", "credential", "fill"],
                       input="protocol=https\nhost=github.com\n\n",
                       env=h, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    assert "password=" not in (p.stdout or "")                          # the default path resolves no cred


def test_token_targets_seam_read_by_driver_absent_from_agent_env(tmp_path):
    # the scope seam is a driver-side config (a list of repo names): the driver reads it from its raw env,
    # but it carries "TOKEN" so it is scrubbed from the agent env (the agent never needs it).
    c = _load()
    raw = {c.TOKEN_TARGETS_ENV: "owner/a, owner/b", "PATH": "/usr/bin"}
    assert c.declared_token_targets(raw) == ["owner/a", "owner/b"]       # driver reads it
    assert c.declared_token_targets({}) == []                           # undeclared -> empty
    assert c.TOKEN_TARGETS_ENV not in c.harden_agent_env(raw, tmp_path / "s")   # agent never sees it


# ── #348 S7: refuse_to_start relaxes ONLY on the GO-gated DELIVER path ──
def test_deliver_allowed_targets_relaxes_only_on_the_deliver_path():
    c = _load()
    unit, delivery = "owner/mono", "owner/ironclad"
    # IMPLEMENT/GATE: the per-unit allowed set is the unit target ONLY -> the delivery target is refused
    assert c.refuse_to_start([delivery], [unit])
    # DELIVER path: deliver_allowed_targets permits the delivery target (used only after a valid GO)
    allowed = c.deliver_allowed_targets(unit, delivery)
    assert set(allowed) == {unit, delivery}
    assert c.refuse_to_start([unit, delivery], allowed) == []
    # a target outside even the relaxed set is still refused on the DELIVER path
    assert c.refuse_to_start(["owner/evil"], allowed)
