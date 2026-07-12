"""Pure tooling-envelope authorization policy.

The policy is default-off. When enabled, a caller must present the canonical coder
spawn tuple: executable identity plus the command template shape to run.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off", ""}
_ASCII_WS_RE = re.compile(r"[ \t\n\r\f\v]+")
_VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([^}]+)\}")
_AUTOPILOT_ALLOWED_STREAM = (
    "{bin} --model {model} --effort {effort} --dangerously-skip-permissions "
    "--verbose --output-format stream-json --print {prompt}"
)
_AUTOPILOT_ALLOWED_NON_STREAM = (
    "{bin} --model {model} --effort {effort} --dangerously-skip-permissions --print {prompt}"
)


@dataclass(frozen=True)
class ToolingEnvelopeEntry:
    bin: str
    cmd_template: str


@dataclass(frozen=True)
class ToolingEnvelopePolicy:
    enabled: bool = False
    allow_list: tuple[ToolingEnvelopeEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Verdict:
    authorized: bool
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.authorized


def _strict_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    return False


def load_tooling_envelope_policy(config: Optional[dict]) -> ToolingEnvelopePolicy:
    """Load ``security.tooling_envelope`` from a config tree, fail-soft and default-off."""
    try:
        section = (((config or {}).get("security") or {}).get("tooling_envelope") or {})
        enabled = _strict_bool(section.get("enabled", False))
        entries = []
        for raw in section.get("allow_list") or []:
            if not isinstance(raw, dict):
                continue
            b = str(raw.get("bin") or "").strip()
            t = _normalize_template(raw.get("cmd_template"))
            if b and t:
                entries.append(ToolingEnvelopeEntry(bin=b, cmd_template=t))
        return ToolingEnvelopePolicy(enabled=enabled, allow_list=tuple(entries))
    except Exception:
        return ToolingEnvelopePolicy()


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
        if not pol.enabled:
            return Verdict(True)
        candidate_bin = _bin_identity(bin)
        candidate_template = _normalize_template(cmd_template)
        if not candidate_bin or not candidate_template:
            return Verdict(False, "tooling envelope refused malformed coder command")
        for entry in pol.allow_list:
            if _bin_matches(candidate_bin, entry.bin) and candidate_template == _normalize_template(entry.cmd_template):
                return Verdict(True)
        return Verdict(False, "tooling envelope refused unauthorized coder command")
    except Exception:
        return Verdict(False, "tooling envelope refused malformed coder command")


def _coerce_policy(policy: Any) -> Optional[ToolingEnvelopePolicy]:
    if isinstance(policy, ToolingEnvelopePolicy):
        if not isinstance(policy.enabled, bool):
            return None
        return policy
    if isinstance(policy, dict):
        if "tooling_envelope" in policy or "security" in policy:
            return load_tooling_envelope_policy(policy)
        if "enabled" not in policy and "allow_list" not in policy:
            return None
        if not isinstance(policy.get("enabled"), bool) or not isinstance(policy.get("allow_list"), list):
            return None
        entries = []
        for raw in policy.get("allow_list"):
            if isinstance(raw, ToolingEnvelopeEntry):
                entries.append(raw)
            elif isinstance(raw, dict):
                b = str(raw.get("bin") or "").strip()
                t = _normalize_template(raw.get("cmd_template"))
                if b and t:
                    entries.append(ToolingEnvelopeEntry(b, t))
        return ToolingEnvelopePolicy(_strict_bool(policy.get("enabled", False)), tuple(entries))
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
    return (
        candidate_identity == allowed_identity
        or os.path.basename(candidate_identity) == os.path.basename(allowed_identity)
    )


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


def _looks_like_autopilot(parts: Sequence[str]) -> str:
    tokens = list(parts)
    names = {"claude", "claude.exe"}
    if len(tokens) < 8 or os.path.basename(tokens[0]).lower() not in names:
        return ""
    if tokens[1] != "--model" or not tokens[2] or tokens[3] != "--effort" or not tokens[4]:
        return ""
    if len(tokens) == 8 and tokens[5:7] == ["--dangerously-skip-permissions", "--print"] and tokens[7]:
        return _AUTOPILOT_ALLOWED_NON_STREAM
    if len(tokens) == 11 and tokens[5:10] == [
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format",
        "stream-json",
        "--print",
    ] and tokens[10]:
        return _AUTOPILOT_ALLOWED_STREAM
    return ""


def autopilot_claude_print_template(stream: bool = True) -> str:
    """The explicit non-provider-template Claude autopilot argv shape."""
    return _AUTOPILOT_ALLOWED_STREAM if stream else _AUTOPILOT_ALLOWED_NON_STREAM
