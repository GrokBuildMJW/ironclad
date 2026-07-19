"""Pure, mandatory tooling-envelope authorization policy."""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence


_ASCII_WS_RE = re.compile(r"[ \t\n\r\f\v]+")
_VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([^}]+)\}")
_WINDOWS_EXECUTABLE_EXTENSIONS = frozenset({".exe", ".cmd", ".bat", ".com", ".ps1"})
DEFAULT_CLI_BIN = os.environ.get("GX10_CLAUDE_BIN", "claude")
DEFAULT_CLI_CMD_TEMPLATE = (
    "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}"
)
_AUTOPILOT_SAFE_STREAM = (
    "{bin} --model {model} --effort {effort} --permission-mode {permission} "
    "--verbose --output-format stream-json --print {prompt}"
)
_AUTOPILOT_SAFE_NON_STREAM = (
    "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}"
)
_AUTOPILOT_SAFE_STREAM_MCP = (
    "{bin} --model {model} --effort {effort} --permission-mode {permission} "
    "--mcp-config {mcp_config} --verbose --output-format stream-json --print {prompt}"
)
_AUTOPILOT_SAFE_NON_STREAM_MCP = (
    "{bin} --model {model} --effort {effort} --permission-mode {permission} "
    "--mcp-config {mcp_config} --print {prompt}"
)
_AUTOPILOT_BYPASS_STREAM = (
    "{bin} --model {model} --effort {effort} --dangerously-skip-permissions "
    "--verbose --output-format stream-json --print {prompt}"
)
_AUTOPILOT_BYPASS_NON_STREAM = (
    "{bin} --model {model} --effort {effort} --dangerously-skip-permissions --print {prompt}"
)
_AUTOPILOT_BYPASS_STREAM_MCP = (
    "{bin} --model {model} --effort {effort} --dangerously-skip-permissions "
    "--mcp-config {mcp_config} --verbose --output-format stream-json --print {prompt}"
)
_AUTOPILOT_BYPASS_NON_STREAM_MCP = (
    "{bin} --model {model} --effort {effort} --dangerously-skip-permissions "
    "--mcp-config {mcp_config} --print {prompt}"
)


@dataclass(frozen=True)
class ToolingEnvelopeEntry:
    bin: str
    cmd_template: str


@dataclass(frozen=True)
class ToolingEnvelopePolicy:
    enabled: bool = True
    allow_list: tuple[ToolingEnvelopeEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Verdict:
    authorized: bool
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.authorized


def load_tooling_envelope_policy(config: Optional[dict]) -> ToolingEnvelopePolicy:
    """Load mandatory policy data; missing data derives from enabled CLI agents."""
    try:
        root = config or {}
        section = ((root.get("security") or {}).get("tooling_envelope") or {})
        if not isinstance(section, dict):
            return ToolingEnvelopePolicy()
        raw_entries = section.get("allow_list")
        if raw_entries is None:
            raw_entries = _derived_entries(root)
        entries = _validated_entries(raw_entries)
        return ToolingEnvelopePolicy(allow_list=entries or ())
    except Exception:
        return ToolingEnvelopePolicy()


def _derived_entries(config: dict) -> list[dict]:
    """Return exact bin/template tuples from enabled CLI ProviderSpecs."""
    out = []
    for key in ("code_agents", "providers"):
        block = config.get(key) or {}
        pool = block.get("pool") if isinstance(block, dict) else None
        if pool is None:
            continue
        if not isinstance(pool, list):
            raise ValueError(f"malformed {key}.pool")
        for spec in pool:
            if not isinstance(spec, dict) or not isinstance(spec.get("enabled", True), bool):
                raise ValueError(f"malformed {key} spec")
            if not spec.get("enabled", True):
                continue
            if str(spec.get("kind") or "").strip().lower() != "cli":
                continue
            effective_bin = spec.get("bin") or DEFAULT_CLI_BIN
            effective_template = spec.get("cmd_template") or DEFAULT_CLI_CMD_TEMPLATE
            capabilities = spec.get("capabilities") or {}
            permission_bypass = (
                isinstance(capabilities, dict) and capabilities.get("permission_bypass") is True
            )
            if "--dangerously-skip-permissions" in str(effective_template) and not permission_bypass:
                raise ValueError("permission bypass template lacks explicit per-agent capability")
            out.append({"bin": effective_bin, "cmd_template": effective_template})
            # The local/server handover lane renders the ProviderSpec template. The
            # engine autopilot lane preserves Claude's stream/non-stream argv shape.
            if os.path.basename(str(effective_bin)).lower() in {"claude", "claude.exe"}:
                template = str(effective_template)
                if "--print" in template:
                    out.extend([
                        {"bin": effective_bin, "cmd_template": _AUTOPILOT_SAFE_STREAM},
                        {"bin": effective_bin, "cmd_template": _AUTOPILOT_SAFE_NON_STREAM},
                        {"bin": effective_bin, "cmd_template": _AUTOPILOT_SAFE_STREAM_MCP},
                        {"bin": effective_bin, "cmd_template": _AUTOPILOT_SAFE_NON_STREAM_MCP},
                    ])
                    if permission_bypass:
                        out.extend([
                            {"bin": effective_bin, "cmd_template": _AUTOPILOT_BYPASS_STREAM},
                            {"bin": effective_bin, "cmd_template": _AUTOPILOT_BYPASS_NON_STREAM},
                            {"bin": effective_bin, "cmd_template": _AUTOPILOT_BYPASS_STREAM_MCP},
                            {"bin": effective_bin, "cmd_template": _AUTOPILOT_BYPASS_NON_STREAM_MCP},
                        ])
    return out


def _validated_entries(raw_entries: Any) -> Optional[tuple[ToolingEnvelopeEntry, ...]]:
    if not isinstance(raw_entries, list):
        return None
    entries = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            return None
        b, t = raw.get("bin"), raw.get("cmd_template")
        if not isinstance(b, str) or not b.strip() or not isinstance(t, (str, list, tuple)):
            return None
        normalized = _normalize_template(t)
        if not normalized:
            return None
        entries.append(ToolingEnvelopeEntry(bin=b.strip(), cmd_template=normalized))
    return tuple(entries)


def assert_authorized(bin: Any, cmd_template: Any, policy: Any) -> Verdict:
    """Return an always-truthful tooling-envelope authorization verdict.

    This function is pure from the caller's point of view and never raises. The
    caller supplies the already-resolved launch tuple when possible; this helper
    canonicalizes it defensively for identity matching. FA-S2 call sites must use
    the verdict directly, e.g. ``verdict = assert_authorized(...)`` followed by
    ``if not verdict: refuse(verdict.reason)``.
    """
    try:
        pol = _coerce_policy(policy)
        if pol is None:
            return Verdict(False, "tooling envelope refused malformed policy")
        candidate_bin = _bin_identity(bin)
        candidate_template = _normalize_template(cmd_template)
        if not candidate_bin or not candidate_template:
            return Verdict(False, "tooling envelope refused malformed coder command")
        for entry in pol.allow_list:
            if _bin_matches(candidate_bin, entry.bin) and candidate_template == _normalize_template(entry.cmd_template):
                return Verdict(True)
        try:
            resolved_bin = repr(bin)
        except Exception:
            resolved_bin = "<unprintable>"
        # /feedback persists this detail as operator-visible durable blocked_reason. An absolute executable
        # path can expose the local install layout; that is an intentional operations-diagnosis tradeoff.
        return Verdict(
            False,
            "tooling envelope refused unauthorized coder command "
            f"(resolved bin={resolved_bin}, cmd_template={candidate_template!r}; "
            "no allow_list entry matched both)",
        )
    except Exception:
        return Verdict(False, "tooling envelope refused malformed coder command")


def _coerce_policy(policy: Any) -> Optional[ToolingEnvelopePolicy]:
    if isinstance(policy, ToolingEnvelopePolicy):
        if policy.enabled is not True:
            return None
        return policy
    if isinstance(policy, dict):
        if "tooling_envelope" in policy or "security" in policy:
            return load_tooling_envelope_policy(policy)
        if "allow_list" not in policy:
            return None
        if policy.get("enabled", True) is not True:
            return None
        entries = _validated_entries(policy.get("allow_list"))
        if entries is None:
            return None
        return ToolingEnvelopePolicy(allow_list=entries)
    return None


def _bin_identity(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        if _is_bare_command(s):
            return os.path.basename(s)
        p = Path(_expand_portable_path(s))
        if p.is_absolute() or not _is_bare_command(s):
            return os.path.realpath(str(p))
    except Exception:
        pass
    return os.path.basename(s)


def _bin_matches(candidate_identity: str, allowed: str) -> bool:
    allowed = str(allowed or "").strip()
    if not candidate_identity or not allowed:
        return False
    allowed_expanded = _expand_portable_path(allowed)
    if any(ch in allowed_expanded for ch in "*?"):
        return _glob_match(candidate_identity, allowed_expanded)
    if not _is_bare_command(allowed_expanded):
        return candidate_identity == _path_identity(allowed_expanded)
    allowed_identity = _bin_identity(allowed_expanded)
    candidate_basename = _portable_basename(candidate_identity)
    allowed_basename = _portable_basename(allowed_identity)
    return (
        candidate_identity == allowed_identity
        or candidate_basename == allowed_basename
        or _windows_executable_stem(candidate_basename).lower() == allowed_basename.lower()
    )


def bin_matches(candidate: Any, allowed: Any) -> bool:
    """Match a resolved CLI binary against an allowed binary across host path styles."""
    return _bin_matches(_bin_identity(candidate), str(allowed or ""))


def _portable_basename(value: str) -> str:
    """Return a basename for POSIX or Windows separators, independent of the client host."""
    return re.split(r"[/\\]", value)[-1]


def _windows_executable_stem(value: str) -> str:
    stem, extension = os.path.splitext(value)
    return stem if extension.lower() in _WINDOWS_EXECUTABLE_EXTENSIONS else value


def _is_bare_command(value: str) -> bool:
    if not value:
        return False
    separators = {os.path.sep, "/", "\\"}
    if os.path.altsep:
        separators.add(os.path.altsep)
    if any(sep and sep in value for sep in separators):
        return False
    return True


def _path_identity(value: str) -> str:
    try:
        return os.path.realpath(str(Path(value)))
    except Exception:
        return ""


def _normalize_template(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _normalize_argv(value)
    try:
        text = str(value)
    except Exception:
        return ""
    text = text.replace("{mcp}", "")
    return _ASCII_WS_RE.sub(" ", text).strip(" \t\n\r\f\v")


def _expand_portable_path(value: str) -> str:
    out = value
    if out == "~" or out.startswith("~/") or out.startswith("~\\"):
        out = str(Path.home()) + out[1:]

    def repl(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2) or ""
        return os.environ.get(key, match.group(0))

    return _VAR_RE.sub(repl, out)


def _glob_match(text: str, pattern: str) -> bool:
    rx = ["^"]
    for ch in pattern:
        if ch == "*":
            rx.append(".*")
        elif ch == "?":
            rx.append(".")
        else:
            rx.append(re.escape(ch))
    rx.append("$")
    return re.fullmatch("".join(rx), text) is not None


def _normalize_argv(argv: Sequence[Any]) -> str:
    try:
        parts = [str(p) for p in argv if str(p or "").strip()]
    except Exception:
        return ""
    if not parts:
        return ""
    autopilot = _looks_like_autopilot(parts)
    if autopilot:
        return autopilot
    return _normalize_template(" ".join(shlex.quote(p) for p in parts))


def _is_value_token(token: str) -> bool:
    """Return whether a model-authored autopilot value slot contains a real value, not a flag.

    The child is spawned without a shell, so a flag-shaped value is inert. Refusing one removes the
    asymmetry between the denylisted ``--dangerously-skip-permissions`` and any other flag and closes model,
    effort, permission-mode, and MCP-config slots uniformly.
    """
    return bool(token) and not token.startswith("--")


def _looks_like_autopilot(parts: Sequence[str]) -> str:
    tokens = list(parts)
    # Extension-insensitive like _bin_matches: a probe-resolved claude.cmd/.exe stem is still `claude`.
    if len(tokens) < 8 or _windows_executable_stem(os.path.basename(tokens[0])).lower() != "claude":
        return ""
    if (tokens[1] != "--model" or not _is_value_token(tokens[2])
            or tokens[3] != "--effort" or not _is_value_token(tokens[4])):
        return ""
    if (len(tokens) == 9 and tokens[5] == "--permission-mode" and _is_value_token(tokens[6])
            and tokens[7] == "--print" and tokens[8]):
        return _AUTOPILOT_SAFE_NON_STREAM
    if len(tokens) == 12 and tokens[5] == "--permission-mode" and _is_value_token(tokens[6]) and tokens[7:11] == [
        "--verbose",
        "--output-format",
        "stream-json",
        "--print",
    ] and tokens[11]:
        return _AUTOPILOT_SAFE_STREAM
    if (len(tokens) == 11 and tokens[5] == "--permission-mode" and _is_value_token(tokens[6])
            and tokens[7] == "--mcp-config" and _is_value_token(tokens[8])
            and tokens[9] == "--print" and tokens[10]):
        return _AUTOPILOT_SAFE_NON_STREAM_MCP
    if (len(tokens) == 14 and tokens[5] == "--permission-mode" and _is_value_token(tokens[6])
            and tokens[7] == "--mcp-config" and _is_value_token(tokens[8]) and tokens[9:13] == [
                "--verbose",
                "--output-format",
                "stream-json",
                "--print",
            ] and tokens[13]):
        return _AUTOPILOT_SAFE_STREAM_MCP
    if len(tokens) == 8 and tokens[5:7] == ["--dangerously-skip-permissions", "--print"] and tokens[7]:
        return _AUTOPILOT_BYPASS_NON_STREAM
    if len(tokens) == 11 and tokens[5:10] == [
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format",
        "stream-json",
        "--print",
    ] and tokens[10]:
        return _AUTOPILOT_BYPASS_STREAM
    if (len(tokens) == 10 and tokens[5] == "--dangerously-skip-permissions"
            and tokens[6] == "--mcp-config" and _is_value_token(tokens[7])
            and tokens[8] == "--print" and tokens[9]):
        return _AUTOPILOT_BYPASS_NON_STREAM_MCP
    if (len(tokens) == 13 and tokens[5] == "--dangerously-skip-permissions"
            and tokens[6] == "--mcp-config" and _is_value_token(tokens[7]) and tokens[8:12] == [
                "--verbose",
                "--output-format",
                "stream-json",
                "--print",
            ] and tokens[12]):
        return _AUTOPILOT_BYPASS_STREAM_MCP
    return ""


def autopilot_claude_print_template(
    stream: bool = True, *, permission_bypass: bool = False, mcp: bool = False,
) -> str:
    """The explicit non-provider-template Claude autopilot argv shape."""
    if permission_bypass:
        if mcp:
            return _AUTOPILOT_BYPASS_STREAM_MCP if stream else _AUTOPILOT_BYPASS_NON_STREAM_MCP
        return _AUTOPILOT_BYPASS_STREAM if stream else _AUTOPILOT_BYPASS_NON_STREAM
    if mcp:
        return _AUTOPILOT_SAFE_STREAM_MCP if stream else _AUTOPILOT_SAFE_NON_STREAM_MCP
    return _AUTOPILOT_SAFE_STREAM if stream else _AUTOPILOT_SAFE_NON_STREAM
