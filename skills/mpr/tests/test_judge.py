"""LLM-Judge-Panel (Spec 08 §5.2) — stubbed (no net): blind/de-blind, parse+reask, self-consistency,
panel median + configured providers. Loaded standalone (eval/ is not a package); judge.py self-bootstraps
eval/ onto sys.path so its `import rubric` resolves."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_JUDGE = Path(__file__).resolve().parents[1] / "eval" / "judge.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


J = _load("mpr_judge_probe", _JUDGE)
_DIMS = J.JUDGED_DIMS


class _Rng:                       # controlled blind-order flip (random() < 0.5 → flip)
    def __init__(self, v):
        self.v = v

    def random(self):
        return self.v


def _vote_json(s1, s2, pw="1"):
    return json.dumps({"answer_1": {d: {"score": s1, "rationale": "r", "cited_axes": ["x"]} for d in _DIMS},
                       "answer_2": {d: {"score": s2, "rationale": "r", "cited_axes": []} for d in _DIMS},
                       "pairwise": {d: pw for d in _DIMS}})


def _fixed(text):
    return lambda prompt, *, system, max_tokens: text


# ── §5.2 structured output: parse + exactly one repair re-ask ─────────────────────────────────────
def test_judge_output_parses_to_schema():
    assert J.parse_judgement(_vote_json(4, 2)) is not None
    assert J.parse_judgement("kein json") is None


def test_pairwise_drops_junk_votes_no_slot2_bias():
    # MPR-EVAL-1 (#503): a non-'1'/'2' pairwise value (junk, '3', None) must be DROPPED, not coerced to
    # '2' (which biased the blind panel toward slot 2).
    dims = list(_DIMS)
    assert len(dims) >= 2
    pw = {dims[0]: "1", dims[1]: "3"}                    # one valid, one junk
    if len(dims) >= 3:
        pw[dims[2]] = "2"                                # another valid
    raw = json.dumps({"answer_1": {d: {"score": 4} for d in _DIMS},
                      "answer_2": {d: {"score": 2} for d in _DIMS},
                      "pairwise": pw})
    out = J.parse_judgement(raw)
    assert out is not None
    assert out["pairwise"].get(dims[0]) == "1"          # valid kept
    assert dims[1] not in out["pairwise"]               # junk '3' DROPPED, not coerced to '2'
    if len(dims) >= 3:
        assert out["pairwise"].get(dims[2]) == "2"      # valid '2' kept


def test_non_dict_pairwise_is_dropped_not_raised():
    # MPR-EVAL-1 (#503): a non-dict 'pairwise' (e.g. a string that happens to contain a dim name as a
    # substring) must be dropped wholesale — never indexed (TypeError) — keeping the parser fail-soft.
    raw = json.dumps({"answer_1": {d: {"score": 4} for d in _DIMS},
                      "answer_2": {d: {"score": 2} for d in _DIMS},
                      "pairwise": _DIMS[0]})            # a bare string, not a dict
    out = J.parse_judgement(raw)
    assert out is not None and out["pairwise"] == {}    # dropped, no exception
    assert J.parse_judgement(json.dumps({"answer_1": {"coverage": {"score": 9}}})) is None   # out of range
    # one murks reply → exactly one repair re-ask → success
    seq = iter(["nicht json", _vote_json(4, 2)])
    call = lambda prompt, *, system, max_tokens: next(seq)               # noqa: E731
    assert J.judge_vote("A", "B", ["x"], call=call, rng=_Rng(0.9)) is not None
    # two murks replies → voice dropped
    assert J.judge_vote("A", "B", ["x"], call=_fixed("murks"), rng=_Rng(0.9)) is None


# ── §5.2 blind order randomised + de-blindable ────────────────────────────────────────────────────
def test_blind_order_randomized_and_deblindable():
    call = _fixed(_vote_json(5, 1))            # the judge always favours the FIRST-SHOWN slot (position bias)
    no_flip = J.judge_vote("A", "B", ["x"], call=call, rng=_Rng(0.9))   # shown_1=A → A scored 5
    flip = J.judge_vote("A", "B", ["x"], call=call, rng=_Rng(0.1))      # shown_1=B → B scored 5
    assert no_flip["a"]["coverage"] == 5.0 and no_flip["b"]["coverage"] == 1.0 and no_flip["flipped"] is False
    assert flip["a"]["coverage"] == 1.0 and flip["b"]["coverage"] == 5.0 and flip["flipped"] is True
    assert no_flip["pairwise"]["coverage"] == "a" and flip["pairwise"]["coverage"] == "b"   # de-blind correct


# ── §5.2 panel aggregates three votes (median) from configured providers ──────────────────────────
def test_judge_panel_aggregates_three_votes():
    voices = [{"provider": "sonnet", "call": _fixed(_vote_json(5, 1))},
              {"provider": "opus", "call": _fixed(_vote_json(3, 1))},
              {"provider": "spark", "call": _fixed(_vote_json(1, 1))}]
    panel = J.judge_panel("A", "B", ["x"], voices=voices, rng=_Rng(0.9))     # no-flip → slot-scoring stable
    assert panel["n_votes"] == 3 and panel["a"]["coverage"] == 3.0           # median(5,3,1)
    assert panel["providers_used"] == ["sonnet", "opus", "spark"]            # from config, not hardcoded


def test_self_consistency_drops_unstable_vote():
    flip = {"on": False}

    def _unstable(prompt, *, system, max_tokens):
        flip["on"] = not flip["on"]
        return _vote_json(5 if flip["on"] else 0, 1)                         # alternating → unstable
    voices = [{"provider": "stable", "call": _fixed(_vote_json(4, 1))},
              {"provider": "bad", "call": _unstable}]
    panel = J.judge_panel("A", "B", ["x"], voices=voices, rng=_Rng(0.9))     # no-flip; instability is per-call
    assert panel["n_votes"] == 1 and panel["dropped_providers"] == ["bad"]   # unstable voice dropped
    assert panel["a"]["coverage"] == 4.0


def test_parse_extracts_json_after_prose_preamble():
    # brace-in-prose preamble + trailing text must not break extraction (raw_decode scan, nesting-safe).
    body = _vote_json(4, 2)
    assert J.parse_judgement("Hinweis {nicht json}\n" + body + "\n— Ende.") is not None
    assert J.parse_judgement("```json\n" + body + "\n```") is not None      # fenced still works


def test_panel_all_dropped_is_empty_and_detectable():
    # every voice unparsable → fail-soft empty panel, detectable via n_votes==0 (caller treats as hard fail).
    panel = J.judge_panel("A", "B", ["x"], voices=[{"provider": "p", "call": _fixed("garbage")}], rng=_Rng(0.9))
    assert panel["n_votes"] == 0 and panel["a"] == {} and panel["pairwise"] == {}
    assert panel["dropped_providers"] == ["p"]


def test_pairwise_tie_breaks_to_a():
    # two kept votes split a/b on a dim → documented arm-A default.
    voices = [{"provider": "v1", "call": _fixed(_vote_json(3, 3, pw="1"))},   # prefers shown-1 (=a, no flip)
              {"provider": "v2", "call": _fixed(_vote_json(3, 3, pw="2"))}]   # prefers shown-2 (=b, no flip)
    panel = J.judge_panel("A", "B", ["x"], voices=voices, rng=_Rng(0.9))
    assert panel["n_votes"] == 2 and panel["pairwise"]["coverage"] == "a"


def test_cli_selftest_runs_clean(capsys):
    J._selftest()
    assert "judge selftest: OK" in capsys.readouterr().out
