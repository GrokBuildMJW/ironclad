"""Test-count generator: docs == real suite (#211, ADR-0007), offline.

Pure parsing/diff/rewrite only (no pytest subprocess). Lives in `scripts/ci/` (private) → skips in an
installed/clean-room tree.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GTC = _REPO / "scripts" / "ci" / "gen_test_counts.py"

pytestmark = pytest.mark.skipif(
    not _GTC.is_file(),
    reason="private CI gen_test_counts.py absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_gtc", _GTC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_parse_summary_is_order_independent():
    gtc = _load()
    assert gtc.parse_py_summary("1038 passed, 9 skipped in 30s") == {"passed": 1038, "skipped": 9}
    assert gtc.parse_py_summary("9 skipped, 1038 passed in 30s") == {"passed": 1038, "skipped": 9}
    assert gtc.parse_py_summary("500 passed in 5s") == {"passed": 500, "skipped": 0}


def test_expected_split():
    gtc = _load()
    assert gtc.expected_counts(1038, 9) == {"offline": 1038, "live": 9, "total": 1047}


_README = "Verified by **1047 Python tests** (1038 offline + 9 live) plus **344 TypeScript client\n"
_REPORT = (
    "| Automated tests (offline, no model) | **1038 passed** |\n"
    "| **Total Python** | **1047** |\n"
    "pytest -q                                   # from core/  → 1038 passed, 9 skipped\n"
    "the **1047** total (1038 offline + 9 live) — now includes the MPR core built-in suite.\n"
    "| **Area one** | `a` | 1038 |\n"   # per-area partition rows summing to the total (1047)
    "| **Live smoke** | `live_smoke` | 9 |\n"
)


def test_documented_counts_extracts_every_claim():
    gtc = _load()
    d = gtc.documented_counts(_README, _REPORT)
    assert d["readme.total"] == 1047 and d["readme.offline"] == 1038 and d["readme.live"] == 9
    assert d["report.passed"] == 1038 and d["report.total"] == 1047
    assert d["report.pytest.passed"] == 1038 and d["report.pytest.skipped"] == 9
    assert d["report.narr.total"] == 1047 and d["report.narr.offline"] == 1038 and d["report.narr.live"] == 9
    assert d["report.area_sum"] == 1047    # the per-area partition sums to the total


def test_diff_is_empty_when_docs_match():
    gtc = _load()
    exp = gtc.expected_counts(1038, 9)
    assert gtc.diff_counts(gtc.documented_counts(_README, _REPORT), exp) == []


def test_diff_flags_each_stale_number():
    gtc = _load()
    exp = gtc.expected_counts(1050, 9)   # suite grew to 1059 total; docs still say 1047
    drift = gtc.diff_counts(gtc.documented_counts(_README, _REPORT), exp)
    assert any("readme.total" in d for d in drift)
    assert any("report.passed" in d for d in drift)
    assert len(drift) >= 5               # every dependent number is flagged


def test_rewrite_updates_all_numbers_and_roundtrips():
    gtc = _load()
    exp = gtc.expected_counts(1050, 11)  # 1061 total
    nr, nrep = gtc.rewrite(_README, _REPORT, exp)
    assert "**1061 Python tests** (1050 offline + 11 live)" in nr
    assert "**1050 passed**" in nrep and "**Total Python** | **1061**" in nrep
    assert "→ 1050 passed, 11 skipped" in nrep
    assert "the **1061** total (1050 offline + 11 live)" in nrep
    # rewrite manages the HEADLINE numbers only (area rows are dev-maintained) → headline diffs gone
    headline_drift = [d for d in gtc.diff_counts(gtc.documented_counts(nr, nrep), exp) if "area_sum" not in d]
    assert headline_drift == []


def test_area_row_sum_adds_only_bare_integer_cells():
    gtc = _load()
    report = (
        "| **Total Python** | **1047** |\n"          # summary row uses **N** → NOT counted
        "| **Agent-Contract-Kernel** ... | `registry` | 87 |\n"
        "| **Misc** | `manual_cat` | 7 |\n"
        "| **Live smoke** | `live_smoke` | 9 |\n"
    )
    assert gtc.area_row_sum(report) == 103   # 87 + 7 + 9, the **1047** summary row excluded


def test_area_sum_mismatch_is_flagged_against_total():
    gtc = _load()
    # docs headline correct (1047) but a per-area row was not bumped → area sum 1040 != total 1047
    report = _REPORT + "| **Some area** | `x` | 1040 |\n"
    exp = gtc.expected_counts(1038, 9)        # total 1047
    drift = gtc.diff_counts(gtc.documented_counts(_README, report), exp)
    assert any("report.area_sum" in d and "bump the right per-area row" in d for d in drift)


def test_skip_guard_flags_a_non_live_skip():
    gtc = _load()
    live = ["SKIPPED [1] ack/tests/test_live_smoke.py:60: set GX10_LIVE_URL"]
    rogue = ["SKIPPED [1] ack/tests/test_something_else.py:5: flaky"]
    assert gtc._assert_skips_are_live(live) == []
    assert gtc._assert_skips_are_live(live + rogue) == rogue
