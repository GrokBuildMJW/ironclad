"""Runtime wiring for the tooling-envelope policy."""
from __future__ import annotations

from typing import Any, Optional

from ack.tooling_envelope import ToolingEnvelopePolicy, assert_authorized


def _policy():
    try:
        import gx10  # type: ignore
        return getattr(gx10, "TOOLING_ENVELOPE_POLICY", None) or ToolingEnvelopePolicy()
    except Exception:
        return ToolingEnvelopePolicy()


def envelope_enabled() -> bool:
    try:
        return bool(getattr(_policy(), "enabled", False))
    except Exception:
        return False


def envelope_policy_public() -> dict:
    """Return the non-secret effective allow-list for client-side local spawn checks."""
    pol = _policy()
    if not getattr(pol, "enabled", False):
        return {"enabled": False, "allow_list": []}
    return {
        "enabled": True,
        "allow_list": [
            {"bin": e.bin, "cmd_template": e.cmd_template}
            for e in getattr(pol, "allow_list", ())
        ],
    }


def _envelope_authorize(bin: Any, template: Any) -> Optional[str]:
    """Return a refusal message, or ``None`` when the resolved launch tuple is authorized."""
    verdict = assert_authorized(bin, template, _policy())
    if verdict:
        return None
    return verdict.reason or "tooling envelope refused coder command"


def _envelope_authorize_spec(spec: Any) -> Optional[str]:
    """Authorize a provider/code-agent spec after applying inherited launch defaults."""
    try:
        from providers import canonical_launch_tuple
        bin_, template = canonical_launch_tuple(spec)
    except Exception:
        bin_, template = None, None
    return _envelope_authorize(bin_, template)
