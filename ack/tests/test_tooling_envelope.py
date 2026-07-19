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
from ack.tooling_envelope import (  # noqa: E402
    DEFAULT_CLI_CMD_TEMPLATE,
    DEFAULT_CLI_BIN,
    Verdict,
    assert_authorized,
    autopilot_claude_print_template,
    load_tooling_envelope_policy,
)


CLAUDE_TEMPLATE = "{bin} --model {model} --effort {effort} --permission-mode {permission} {mcp} --print {prompt}"
_MCP_CONFIG = '{"mcpServers":{"memory":{"command":"python"}}}'


def _extended_autopilot_policy():
    return load_tooling_envelope_policy({
        "code_agents": {"pool": [{
            "kind": "cli",
            "bin": "claude",
            "cmd_template": CLAUDE_TEMPLATE,
            "capabilities": {"permission_bypass": True},
        }]},
    })


def _autopilot_argv(*, stream: bool, permission_bypass: bool, mcp: bool) -> list[str]:
    argv = ["claude", "--model", "claude-opus-4-8", "--effort", "high"]
    if permission_bypass:
        argv.append("--dangerously-skip-permissions")
    else:
        argv.extend(["--permission-mode", "default"])
    if mcp:
        argv.extend(["--mcp-config", _MCP_CONFIG])
    if stream:
        argv.extend(["--verbose", "--output-format", "stream-json"])
    return argv + ["--print", "--handover text"]


def test_missing_policy_data_denies_everything():
    policy = load_tooling_envelope_policy({})
    assert policy.enabled is True
    verdict = assert_authorized("anything", "not a configured template", policy)
    assert isinstance(verdict, Verdict)
    assert not verdict


def test_legacy_enabled_is_ignored_and_template_is_normalized():
    policy = load_tooling_envelope_policy({
        "security": {
            "tooling_envelope": {
                "enabled": "false",
                "allow_list": [{"bin": "claude", "cmd_template": CLAUDE_TEMPLATE}],
            }
        }
    })
    assert policy.enabled is True
    assert policy.allow_list[0].cmd_template == (
        "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}"
    )

    garbage = load_tooling_envelope_policy({"security": {"tooling_envelope": {"enabled": "maybe"}}})
    assert garbage.enabled is True
    assert not assert_authorized("claude", CLAUDE_TEMPLATE, garbage)


def test_omitted_allow_list_derives_exact_enabled_cli_specs():
    cfg = {"code_agents": {"pool": [
        {"kind": "cli", "enabled": True, "bin": "claude", "cmd_template": CLAUDE_TEMPLATE},
        {"kind": "cli", "enabled": False, "bin": "other", "cmd_template": "{bin} {prompt}"},
    ]}, "providers": {"pool": [
        {"kind": "cli", "enabled": True, "bin": "tool"},
        {"kind": "cli", "enabled": True, "cmd_template": "{bin} custom {prompt}"},
    ]}}
    policy = load_tooling_envelope_policy(cfg)
    assert assert_authorized("claude", CLAUDE_TEMPLATE, policy)
    assert assert_authorized("/usr/local/bin/claude", CLAUDE_TEMPLATE, policy)
    assert assert_authorized("claude.exe", CLAUDE_TEMPLATE, policy)
    assert not assert_authorized("other", "{bin} {prompt}", policy)
    assert not assert_authorized("claude-evil", CLAUDE_TEMPLATE, policy)
    assert assert_authorized("tool", DEFAULT_CLI_CMD_TEMPLATE, policy)
    assert assert_authorized(DEFAULT_CLI_BIN, "{bin} custom {prompt}", policy)
    assert assert_authorized("claude", [
        "claude", "--model", "claude-sonnet-5", "--effort", "high",
        "--permission-mode", "default", "--print", "handover text",
    ], policy)
    assert not assert_authorized("claude", [
        "claude", "--model", "claude-sonnet-5", "--effort", "high",
        "--dangerously-skip-permissions", "--print", "handover text",
    ], policy)


def test_explicit_empty_and_malformed_allow_lists_deny_all():
    for allow_list in ([], "not-a-list", [{"bin": "claude"}]):
        policy = load_tooling_envelope_policy({
            "security": {"tooling_envelope": {"allow_list": allow_list}},
            "code_agents": {"pool": [{"kind": "cli", "bin": "claude", "cmd_template": CLAUDE_TEMPLATE}]},
        })
        assert not assert_authorized("claude", CLAUDE_TEMPLATE, policy)


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
    assert "unauthorized coder command" in verdict.reason
    assert f"resolved bin={str(trusted)!r}" in verdict.reason
    assert f"cmd_template={CLAUDE_TEMPLATE.replace(' {mcp}', '')!r}" in verdict.reason


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
    assert "unauthorized coder command" in verdict.reason
    assert f"resolved bin={str(attacker)!r}" in verdict.reason
    assert f"cmd_template={CLAUDE_TEMPLATE.replace(' {mcp}', '')!r}" in verdict.reason


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


@pytest.mark.parametrize(
    ("stream", "permission_bypass", "mcp"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (False, False, True),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
    ids=[
        "safe-non-stream",
        "safe-stream",
        "bypass-non-stream",
        "bypass-stream",
        "safe-non-stream-mcp",
        "safe-stream-mcp",
        "bypass-non-stream-mcp",
        "bypass-stream-mcp",
    ],
)
def test_all_autopilot_argv_shapes_are_explicitly_authorized(stream, permission_bypass, mcp):
    policy = _extended_autopilot_policy()
    template = autopilot_claude_print_template(
        stream=stream, permission_bypass=permission_bypass, mcp=mcp,
    )

    assert any(entry.cmd_template == template for entry in policy.allow_list)
    assert bool(assert_authorized(
        "claude",
        _autopilot_argv(stream=stream, permission_bypass=permission_bypass, mcp=mcp),
        policy,
    ))


@pytest.mark.parametrize(
    "argv",
    [
        _autopilot_argv(stream=False, permission_bypass=False, mcp=True)[:7]
        + ["--evil-exfil", "https://attacker.invalid", "--print", "handover text"],
        _autopilot_argv(stream=False, permission_bypass=False, mcp=True)[:8]
        + ["--dangerously-skip-permissions", "--print", "handover text"],
        _autopilot_argv(stream=False, permission_bypass=False, mcp=True) + ["extra-token"],
        _autopilot_argv(stream=False, permission_bypass=False, mcp=True)[:8]
        + ["--print", "handover text"],
    ],
    ids=["bogus-mcp-flag", "bypass-smuggled-into-safe", "extra-token", "mcp-without-value"],
)
def test_mcp_autopilot_shape_refuses_noncanonical_slot_or_count(argv):
    assert not assert_authorized("claude", argv, _extended_autopilot_policy())


@pytest.mark.parametrize(
    ("slot", "argv"),
    [
        ("permission-mode", [
            "claude", "--model", "claude-opus-4-8", "--effort", "high",
            "--permission-mode", "--dangerously-skip-permissions", "--print", "handover text",
        ]),
        ("mcp-config", [
            "claude", "--model", "claude-opus-4-8", "--effort", "high",
            "--dangerously-skip-permissions", "--mcp-config", "--add-dir",
            "--print", "handover text",
        ]),
        ("model", [
            "claude", "--model", "--verbose", "--effort", "high",
            "--permission-mode", "default", "--print", "handover text",
        ]),
        ("effort", [
            "claude", "--model", "claude-opus-4-8", "--effort", "--verbose",
            "--permission-mode", "default", "--print", "handover text",
        ]),
    ],
)
def test_autopilot_argv_refuses_flag_shaped_value(slot, argv):
    verdict = assert_authorized("claude", argv, _extended_autopilot_policy())

    assert not bool(verdict), slot


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
        "--permission-mode", "default", "--verbose", "--output-format", "stream-json",
        "--print", "handover text",
    ]

    assert not assert_authorized("claude", canonical + ["--mcp-config", "/tmp/evil.json"], policy)
    assert not assert_authorized(
        "claude",
        canonical[:5] + ["--settings", "/tmp/evil.json"] + canonical[5:],
        policy,
    )


def test_apply_config_derives_policy_and_legacy_tombstones_cannot_disable(monkeypatch, capsys):
    cfg = gx10._code_defaults()
    gx10._apply_config(cfg)
    assert gx10.TOOLING_ENVELOPE_POLICY.enabled is True
    assert assert_authorized("claude", cfg["code_agents"]["pool"][0]["cmd_template"],
                             gx10.TOOLING_ENVELOPE_POLICY)

    cfg["security"]["tooling_envelope"] = {
        "enabled": True,
        "allow_list": [{"bin": "claude", "cmd_template": CLAUDE_TEMPLATE}],
    }
    gx10._apply_config(cfg)
    assert gx10.TOOLING_ENVELOPE_POLICY.enabled is True
    assert assert_authorized("claude", CLAUDE_TEMPLATE, gx10.TOOLING_ENVELOPE_POLICY)

    cfg = gx10._code_defaults()
    monkeypatch.setenv("GX10_TOOLING_ENVELOPE_ENABLED", "0")
    gx10._apply_env(cfg)
    gx10._apply_config(cfg)
    assert gx10.TOOLING_ENVELOPE_POLICY.enabled is True
    assert "retired and ignored" in capsys.readouterr().out


def test_tooling_envelope_config_tombstone_warns_once_and_runtime_set_refuses(monkeypatch, capsys):
    cfg = gx10._code_defaults()
    cfg["security"]["tooling_envelope"]["enabled"] = False
    gx10._apply_config(cfg)
    gx10._apply_config(cfg)
    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1 and "security.tooling_envelope.enabled" in warnings[0]
    assert cfg["security"]["tooling_envelope"] == {"allow_list": None}

    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set security.tooling_envelope.enabled false")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]


def test_tooling_allow_list_is_boot_only(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set security.tooling_envelope.allow_list []")
    assert len(surfaced) == 1 and "boot-only" in surfaced[0]
