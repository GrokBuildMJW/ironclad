"""Injected in-core credential hardening for the CLI-runner lane (epic #1043 / #1052).

Locks the CORE equivalent of the private devprocess credential contract for the ``web_search`` /
``read_offload`` lane: the coder subprocess must never inherit the server's secrets, and the ambient push
credential must be unreachable on the default git/gh path — WITHOUT touching ``HOME`` or setting
``CLAUDE_CONFIG_DIR`` (else the coder's own OAuth auth breaks, #994/#996). The load-bearing acceptance:
``default_cli_runner``'s child env carries no secret NAME.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import agent_env  # noqa: E402
import client  # noqa: E402


def test_scrub_removes_secrets_keeps_benign():
    env = {"PATH": "/usr/bin", "HOME": "/h",
           "GH_TOKEN": "x", "GX10_SERVER_TOKEN": "t", "MY_API_SECRET": "s",  # gitleaks:allow — NAMES only
           "SOME_PASSWORD": "p", "BRAVE_API_KEY": "k", "GITHUB_PAT": "z"}
    scrubbed = agent_env.scrub_agent_env(env)
    assert set(scrubbed) == {"PATH", "HOME"}          # every secret-shaped name removed, benign kept
    assert agent_env.leaked_secrets(scrubbed) == []
    assert agent_env.leaked_secrets(env)              # the original leaks


def test_pat_names_evade_the_regex_but_are_caught_explicitly():
    # GH_PAT / GITHUB_PAT carry no TOKEN/KEY/SECRET substring → ONLY the explicit allow-list catches them.
    for pat in ("GH_PAT", "GITHUB_PAT"):
        assert not agent_env._SENSITIVE_RE.search(pat)     # the heuristic alone would miss it
        assert agent_env._is_sensitive(pat)                # the explicit list catches it
        assert pat not in agent_env.scrub_agent_env({pat: "v", "PATH": "/b"})


def test_benign_keyish_names_are_not_flagged():
    for benign in ("KEYBOARD", "MONKEY", "PATH", "HOME", "USERPROFILE", "LANG", "SYSTEMROOT"):
        assert not agent_env._is_sensitive(benign)


def test_harden_redirects_credentials_keeps_home_no_claude_config_dir(tmp_path):
    env = {"PATH": "/usr/bin", "HOME": "/real/home",
           "GH_TOKEN": "t", "GX10_SERVER_TOKEN": "s"}          # gitleaks:allow — NAMES only
    h = agent_env.harden_agent_env(env, tmp_path / "scratch")
    assert not (set(agent_env.SECRET_ENV_VARS) & set(h))        # no declared secret survives
    assert h["HOME"] == "/real/home"                           # HOME preserved (coder ~/.claude OAuth)
    assert "CLAUDE_CONFIG_DIR" not in h                        # never set (pinning it breaks coder auth)
    assert h["GIT_CONFIG_NOSYSTEM"] == "1" and h["GIT_TERMINAL_PROMPT"] == "0"
    assert h["GCM_INTERACTIVE"] == "never" and h["GCM_CREDENTIAL_STORE"] == "plaintext"
    assert Path(h["GCM_PLAINTEXT_STORE_PATH"]).is_dir()        # empty store → a helper reads nothing
    cfg = Path(h["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
    assert "[credential]" in cfg and "helper =" in cfg         # helper list reset to empty
    assert Path(h["GH_CONFIG_DIR"]).is_dir()
    # the leak audit must ignore the deliberately-set selectors (GCM_CREDENTIAL_STORE matches CREDENTIAL)
    assert "GCM_CREDENTIAL_STORE" in agent_env.leaked_secrets(h)                    # flagged without exemption
    assert agent_env.leaked_secrets(h, ignore=agent_env.HARDENING_KEYS) == []       # clean with it


def test_leaked_secrets_ignore_does_not_mask_a_real_secret():
    env = {"GCM_CREDENTIAL_STORE": "cache", "GH_TOKEN": "t"}   # gitleaks:allow — one selector + one token name
    assert agent_env.leaked_secrets(env, ignore=agent_env.HARDENING_KEYS) == ["GH_TOKEN"]


def test_default_cli_runner_child_env_has_no_secrets(monkeypatch, tmp_path):
    # THE acceptance (#1052): a coder spawned via the runner sees NONE of a set of known secret env names,
    # keeps its HOME (OAuth) + PYTHONIOENCODING, and is never given CLAUDE_CONFIG_DIR.
    secrets = {"GH_TOKEN": "x", "GX10_SERVER_TOKEN": "t", "ANTHROPIC_API_KEY": "a",  # gitleaks:allow — NAMES
               "NEO4J_PASSWORD": "p", "GITHUB_PAT": "z"}
    for name, val in secrets.items():
        monkeypatch.setenv(name, val)
    monkeypatch.setenv("GX10_HOME", str(tmp_path / "home"))     # scratch under a temp state root (hermetic)
    monkeypatch.setattr(client, "_HARDENED_CHILD_ENV", None, raising=False)  # rebuild the cache under this env

    captured: dict = {}

    def _fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(client.subprocess, "run", _fake_run)
    spec = types.SimpleNamespace(cmd_template="{bin} --model {model} --print {prompt}",
                                 bin="claude", model="m", permission_mode="plan")
    import gx10
    from ack.tooling_envelope import load_tooling_envelope_policy
    monkeypatch.setattr(gx10, "TOOLING_ENVELOPE_POLICY", load_tooling_envelope_policy({
        "security": {"tooling_envelope": {"allow_list": [
            {"bin": spec.bin, "cmd_template": spec.cmd_template},
        ]}}
    }))

    res = client.default_cli_runner(spec, "hello", effort="high")

    assert res["ok"] is True
    child = captured["env"]
    assert agent_env.leaked_secrets(child, ignore=agent_env.HARDENING_KEYS) == []   # no secret NAME reaches it
    for name in secrets:
        assert name not in child
    assert child.get("PYTHONIOENCODING") == "utf-8"            # still set (encoding preserved)
    assert "CLAUDE_CONFIG_DIR" not in child                    # never pinned
    assert child.get("GIT_CONFIG_NOSYSTEM") == "1"             # the credential redirect is applied
