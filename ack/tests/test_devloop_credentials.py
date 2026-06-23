"""Driver credential contract + IMPLEMENT tool-fence (epic #262, S11 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins that the agent
env is scrubbed of every token (incl. the marker key K), that the tool-fence rejects merge/push/
release verbs while allowing benign commands, and that a mis-scoped driver token is refused at start.
"""
from __future__ import annotations

import importlib.util
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
