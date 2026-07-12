from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ack.tooling_envelope import assert_authorized


VECTORS = Path(__file__).resolve().parents[1] / "tooling_envelope_vectors.json"


def _policy(case: dict[str, Any]) -> Any:
    if "policy" in case:
        return case["policy"]
    return {"enabled": True, "allow_list": case["allow_list"]}


def test_shared_tooling_envelope_vectors(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TE_BIN", "claude")
    monkeypatch.setenv("IRONCLAD_TE_NESTED", "$IRONCLAD_TE_BIN")
    monkeypatch.delenv("IRONCLAD_TE_UNDEFINED", raising=False)

    cases = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert cases
    for case in cases:
        verdict = assert_authorized(case.get("bin"), case.get("cmd_template"), _policy(case))
        assert bool(verdict) is case["expected_authorized"], case["name"]


def test_shared_vector_corpus_is_the_contract_file():
    assert VECTORS.is_file()
    assert os.path.normpath(str(VECTORS)).endswith(os.path.normpath("ack/tooling_envelope_vectors.json"))
