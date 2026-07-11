from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from providers import ProviderKind, ProviderSpec, model_advertised, parse_advertised_models, validate_model  # noqa: E402


def _spec(**kw):
    return ProviderSpec(
        provider_id="grok",
        kind=ProviderKind.CLI,
        agent_id="GROK",
        model=kw.pop("model", "grok-4.5"),
        bin="grok",
        cmd_template="{bin} -m {model} {prompt}",
        **kw,
    )


def test_model_advertised_uses_model_token_boundaries():
    assert model_advertised("grok-4.5", "models:\n- grok-4.5\n- grok-composer-2.5-fast")
    assert not model_advertised("grok-4.5", "grok-build")
    assert not model_advertised("grok-4.5", "grok-4.55")


def test_parse_advertised_models_default_and_pattern():
    assert "grok-4.5" in parse_advertised_models("available: grok-4.5, grok-composer-2.5-fast")
    assert parse_advertised_models("id=grok-4.5\nid=grok-build", r"id=([A-Za-z0-9._-]+)") == [
        "grok-4.5",
        "grok-build",
    ]


def test_validate_model_skips_when_probe_unset_or_output_empty():
    assert validate_model(_spec(), "grok-4.5") is None
    assert validate_model(_spec(models_probe="models"), "") is None


def test_validate_model_reports_ok_and_mismatch():
    ok = validate_model(_spec(models_probe="models"), "grok-4.5\ngrok-composer-2.5-fast")
    bad = validate_model(_spec(models_probe="models", model="grok-build"), "grok-4.5\ngrok-composer-2.5-fast")
    assert ok is not None and ok.ok is True and ok.configured == "grok-4.5"
    assert bad is not None and bad.ok is False and "grok-4.5" in bad.available
