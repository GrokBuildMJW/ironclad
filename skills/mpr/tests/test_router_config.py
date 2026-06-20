"""Router config (skills/mpr/config.py) + config-aware classify (Spec 04 §10 / §3.5 two-tier).

Defaults mirror router.py constants; each key overrides; validation bites (min_panel>=hard-floor,
min_panel<=max_panel, sim in [0,1], temperature>=0, positive ints). classify(config=…) actually
changes behaviour (min_panel/max_panel/distinct_max_sim) while config=None stays the default path.
"""
from __future__ import annotations

import pytest
from _router_fakes import FakeClassifierLLM, persp, registry, run_panel
from pydantic import ValidationError

from mpr.config import RouterConfig, load_router_config
from mpr.router import (
    DISTINCT_MAX_SIM,
    MAX_PANEL,
    MIN_PANEL,
    MIN_QUERY_CHARS,
    ROUTER_MAX_TOKENS,
    ROUTER_TEMPERATURE,
    classify,
)
from mpr.schema import Decision, RouterInput


# ── config schema ────────────────────────────────────────────────────────────────────────────────
def test_defaults_mirror_router_constants():
    c = load_router_config(None)
    assert c.model is None
    assert c.max_tokens == ROUTER_MAX_TOKENS and c.temperature == ROUTER_TEMPERATURE
    assert c.min_panel == MIN_PANEL == 3 and c.max_panel == MAX_PANEL == 7
    assert c.distinct_max_sim == DISTINCT_MAX_SIM == 0.6 and c.min_query_chars == MIN_QUERY_CHARS == 12


def test_empty_section_is_defaults():
    assert load_router_config({}) == load_router_config(None)


def test_override_each_key():
    c = load_router_config({"model": "qwen-small", "max_tokens": 512, "temperature": 0.0,
                            "min_panel": 4, "max_panel": 6, "distinct_max_sim": 0.5,
                            "min_query_chars": 20})
    assert (c.model, c.max_tokens, c.temperature) == ("qwen-small", 512, 0.0)
    assert (c.min_panel, c.max_panel, c.distinct_max_sim, c.min_query_chars) == (4, 6, 0.5, 20)


def test_min_panel_below_hard_floor_rejected():
    with pytest.raises(ValidationError, match="hard floor"):
        load_router_config({"min_panel": 1})  # < _PANEL_HARD_FLOOR=2


def test_min_panel_above_max_rejected():
    with pytest.raises(ValidationError, match="must be <= max_panel"):
        load_router_config({"min_panel": 7, "max_panel": 5})


def test_distinct_sim_out_of_range_rejected():
    with pytest.raises(ValidationError, match=r"\[0, 1\]"):
        load_router_config({"distinct_max_sim": 1.4})


def test_temperature_negative_rejected():
    with pytest.raises(ValidationError, match="temperature must be"):
        load_router_config({"temperature": -0.1})


def test_non_positive_ints_rejected():
    with pytest.raises(ValidationError):
        load_router_config({"max_tokens": 0})
    with pytest.raises(ValidationError):
        load_router_config({"min_query_chars": 0})


def test_extra_key_forbidden():
    with pytest.raises(ValidationError):
        RouterConfig(bogus=1)


# ── config-aware classify ────────────────────────────────────────────────────────────────────────
def _adhoc_three():
    return run_panel(domain="adhoc", mode="evidence-research", perspectives=[
        persp("Eins", "erste eigenständige Brille"), persp("Zwei", "zweite andere Brille"),
        persp("Drei", "dritte wieder andere Brille")])


def test_config_min_panel_raises_threshold_to_decline():
    # default min_panel=3 → a 3-role adhoc runs; min_panel=4 → the same panel declines.
    inp = RouterInput(query="Sollen wir diese Nische breit bewerten?")
    assert classify(inp, llm=FakeClassifierLLM(_adhoc_three()), registry=registry()).decision == Decision.RUN
    d = classify(inp, llm=FakeClassifierLLM(_adhoc_three()), registry=registry(),
                 config=load_router_config({"min_panel": 4}))
    assert d.decision == Decision.DECLINE
    assert d.decline_reason == "insufficient distinct perspectives (3<4)"


def test_config_max_panel_caps_lower():
    five = run_panel(domain="adhoc", mode="evidence-research", perspectives=[
        persp(f"R{i}", t) for i, t in enumerate(
            ["Sicherheit Angriffsfläche", "Performance Latenz", "Kosten Budget",
             "Recht Compliance", "Markt Wettbewerb"])])
    d = classify(RouterInput(query="Sollen wir das umfassend bewerten?"),
                 llm=FakeClassifierLLM(five), registry=registry(),
                 config=load_router_config({"max_panel": 3}))
    assert d.decision == Decision.RUN and len(d.perspectives) == 3


def test_config_none_is_unchanged_default_path():
    inp = RouterInput(query="Sollen wir diese Nische breit bewerten?")
    d1 = classify(inp, llm=FakeClassifierLLM(_adhoc_three()), registry=registry())
    d2 = classify(inp, llm=FakeClassifierLLM(_adhoc_three()), registry=registry(),
                  config=load_router_config(None))
    assert d1.model_dump_json() == d2.model_dump_json()  # config=None == constant path
