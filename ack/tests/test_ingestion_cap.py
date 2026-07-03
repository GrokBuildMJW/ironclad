"""L1-choke (#1046, epic #1043): the single run-loop ingestion choke-point cap.

Every INGESTION tool result (read_file / list_directory / search_files / execute_command) is capped to the
live per-turn budget at ONE place, so a single tool result can never overflow the window — not just
read_file (which caps itself) but the others AND the local-bridge return (which returns before read_file's
own cap). Non-ingestion tools (web_search / parallel_reason / MPR / memory) return already-budgeted or
structured JSON payloads and are never touched.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def test_ingestion_tools_capped_to_budget():
    cap = 2000
    big = "X" * 50_000
    for name in ("search_files", "list_directory", "execute_command", "read_file"):
        out = gx10._cap_ingested_result(name, big, cap)
        assert len(out) < len(big)
        assert "chars omitted" in out and "search_files" in out       # capped + steers to search_files
        assert out.startswith("X") and out.endswith("X")              # head+tail preserved
        assert out.count("X") <= cap + 10                             # retained content bounded by the budget


def test_non_ingestion_tools_never_capped():
    cap = 2000
    big = "Y" * 50_000
    for name in ("web_search", "parallel_reason", "mpr_research", "memory_search", "deep_query_memory"):
        assert gx10._cap_ingested_result(name, big, cap) == big       # structured/budgeted payloads untouched


def test_short_result_passes_through():
    assert gx10._cap_ingested_result("search_files", "small", 2000) == "small"


def test_idempotent_with_read_file_internal_cap():
    # read_file already returns head + its OWN marker + tail (~cap + marker). The choke cap must NOT
    # re-truncate it (the slack covers the marker) → no double marker.
    cap = 3000
    already = "H" * 2000 + "\n\n... [Ironclad: 100 chars omitted — capped] ...\n\n" + "T" * 1000
    assert gx10._cap_ingested_result("read_file", already, cap) == already


def test_repro_many_large_reads_each_bounded():
    # the #366-shaped repro: many large ingestion results in ONE turn — each is bounded by the live budget,
    # so no single append can overflow the wall (cumulative growth across a turn is L3's job, tracked
    # separately as L3-proactive). This pins the per-result guarantee the L1 choke-point provides.
    cap = 1500
    results = [gx10._cap_ingested_result("search_files", "Z" * 40_000, cap) for _ in range(12)]
    assert all(r.count("Z") <= cap + 10 for r in results)
    assert all("chars omitted" in r for r in results)
