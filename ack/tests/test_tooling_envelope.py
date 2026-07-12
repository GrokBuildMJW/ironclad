from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from ack.tooling_envelope import (  # noqa: E402
    Verdict,
    assert_authorized,
    autopilot_claude_print_template,
    load_tooling_envelope_policy,
)


CLAUDE_TEMPLATE = "{bin} --model {model} --effort {effort} --permission-mode {permission} {mcp} --print {prompt}"


def test_policy_default_off_allows_everything():
    policy = load_tooling_envelope_policy({})
    assert policy.enabled is False
    verdict = assert_authorized("anything", "not a configured template", policy)
    assert isinstance(verdict, Verdict)
    assert verdict
    assert verdict.reason is None


def test_loader_strict_bool_and_normalizes_templates():
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": "false",
                "allow_list": [{"bin": "claude", "cmd_template": CLAUDE_TEMPLATE}],
            }
        }
    })
    assert policy.enabled is False
    assert policy.allow_list[0].cmd_template == (
        "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}"
    )

    garbage = load_tooling_envelope_policy({"security": {"tooling_envelope": {"enabled": "maybe"}}})
    assert garbage.enabled is False


def test_authorized_tuple_allows_by_basename_and_normalized_template(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": True,
                "allow_list": [{"bin": "claude.exe", "cmd_template": CLAUDE_TEMPLATE}],
            }
        }
    })
    assert assert_authorized(str(exe), CLAUDE_TEMPLATE.replace(" {mcp}", ""), policy)


def test_undefined_env_allow_list_path_stays_literal_and_refuses(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted" / "claude-a"
    trusted.parent.mkdir()
    trusted.write_text("", encoding="utf-8")
    monkeypatch.delenv("IRONCLAD_UNDEFINED_BIN_DIR", raising=False)
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": True,
                "allow_list": [{"bin": "$IRONCLAD_UNDEFINED_BIN_DIR/claude-a", "cmd_template": CLAUDE_TEMPLATE}],
            }
        }
    })

    verdict = assert_authorized(str(trusted), CLAUDE_TEMPLATE, policy)
    assert not verdict
    assert verdict.reason == "tooling envelope refused unauthorized coder command"


def test_bare_allow_list_entry_ignores_same_named_cwd_file(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted" / "claude.exe"
    planted_cwd = tmp_path / "cwd" / "claude.exe"
    other_cwd = tmp_path / "other-cwd"
    trusted.parent.mkdir()
    planted_cwd.parent.mkdir()
    other_cwd.mkdir()
    trusted.write_text("", encoding="utf-8")
    planted_cwd.write_text("", encoding="utf-8")
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": True,
                "allow_list": [{"bin": "claude.exe", "cmd_template": CLAUDE_TEMPLATE}],
            }
        }
    })

    monkeypatch.chdir(planted_cwd.parent)
    assert assert_authorized(str(trusted), CLAUDE_TEMPLATE, policy)

    monkeypatch.chdir(other_cwd)
    assert assert_authorized(str(trusted), CLAUDE_TEMPLATE, policy)


def test_pinned_binary_path_requires_exact_identity(tmp_path):
    trusted = tmp_path / "trusted" / "claude.exe"
    attacker = tmp_path / "attacker" / "claude.exe"
    trusted.parent.mkdir()
    attacker.parent.mkdir()
    trusted.write_text("", encoding="utf-8")
    attacker.write_text("", encoding="utf-8")
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": True,
                "allow_list": [{"bin": str(trusted), "cmd_template": CLAUDE_TEMPLATE}],
            }
        }
    })

    assert assert_authorized(str(trusted), CLAUDE_TEMPLATE, policy)
    verdict = assert_authorized(str(attacker), CLAUDE_TEMPLATE, policy)
    assert not verdict
    assert verdict.reason == "tooling envelope refused unauthorized coder command"


def test_unauthorized_and_malformed_refuse_without_crashing():
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": True,
                "allow_list": [{"bin": "claude", "cmd_template": CLAUDE_TEMPLATE}],
            }
        }
    })
    assert not assert_authorized("python", CLAUDE_TEMPLATE, policy)
    assert not assert_authorized(None, object(), policy)


def test_malformed_policy_argument_fails_closed():
    class RaisingGetattr:
        def __getattr__(self, name):
            raise RuntimeError(name)

    for bad_policy in (None, RaisingGetattr()):
        verdict = assert_authorized("claude", CLAUDE_TEMPLATE, bad_policy)
        assert not verdict
        assert verdict.reason in {
            "tooling envelope refused malformed policy",
            "tooling envelope refused malformed coder command",
        }


def test_autopilot_argv_shape_is_explicitly_authorized():
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": True,
                "allow_list": [{"bin": "claude", "cmd_template": autopilot_claude_print_template()}],
            }
        }
    })
    argv = [
        "claude", "--model", "claude-sonnet-5", "--effort", "high",
        "--dangerously-skip-permissions", "--verbose", "--output-format", "stream-json",
        "--print", "handover text",
    ]
    assert assert_authorized("claude", argv, policy)


def test_default_autopilot_non_stream_argv_shape_is_explicitly_authorized():
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": True,
                "allow_list": [{"bin": "claude", "cmd_template": autopilot_claude_print_template(stream=False)}],
            }
        }
    })
    argv = [
        "claude", "--model", "claude-sonnet-5", "--effort", "high",
        "--dangerously-skip-permissions", "--print", "handover text",
    ]
    assert assert_authorized("claude", argv, policy)


def test_autopilot_argv_refuses_smuggled_extra_flags():
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": True,
                "allow_list": [{"bin": "claude", "cmd_template": autopilot_claude_print_template()}],
            }
        }
    })
    canonical = [
        "claude", "--model", "claude-sonnet-5", "--effort", "high",
        "--dangerously-skip-permissions", "--verbose", "--output-format", "stream-json",
        "--print", "handover text",
    ]

    assert not assert_authorized("claude", canonical + ["--mcp-config", "/tmp/evil.json"], policy)
    assert not assert_authorized(
        "claude",
        canonical[:5] + ["--settings", "/tmp/evil.json"] + canonical[5:],
        policy,
    )


def test_apply_config_loads_default_off_policy_and_dev1_opt_in(monkeypatch):
    cfg = gx10._code_defaults()
    gx10._apply_config(cfg)
    assert gx10.TOOLING_ENVELOPE_POLICY.enabled is False

    cfg["security"]["tooling_envelope"] = {
        "enabled": True,
        "allow_list": [{"bin": "claude", "cmd_template": CLAUDE_TEMPLATE}],
    }
    gx10._apply_config(cfg)
    assert gx10.TOOLING_ENVELOPE_POLICY.enabled is True
    assert assert_authorized("claude", CLAUDE_TEMPLATE, gx10.TOOLING_ENVELOPE_POLICY)

    cfg = gx10._code_defaults()
    monkeypatch.setenv("GX10_TOOLING_ENVELOPE_ENABLED", "1")
    gx10._apply_env(cfg)
    gx10._apply_config(cfg)
    assert gx10.TOOLING_ENVELOPE_POLICY.enabled is True
