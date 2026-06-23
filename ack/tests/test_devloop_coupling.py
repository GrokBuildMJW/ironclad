"""Machine-gated dev-loop process-coupling guards (epic #262, S3 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Each coupling guard
gets its positive AND negative case (ADR-0007): branch format, exactly-one-issue, C0-present,
code=>test co-presence, capability=>docs.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_COUPLING = _REPO / "scripts" / "devloop" / "coupling.py"

pytestmark = pytest.mark.skipif(
    not _COUPLING.is_file(),
    reason="private dev-loop coupling guards (scripts/devloop/coupling.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_coupling", _COUPLING)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_branch_name_valid():
    c = _load()
    assert c.branch_name_valid("feat/devloop-coupling-265")
    assert c.branch_name_valid("fix/mirror-liveness-258")
    assert not c.branch_name_valid("my-random-branch")
    assert not c.branch_name_valid("feat/no-issue-number")


def test_exactly_one_linked_issue():
    c = _load()
    assert c.exactly_one_linked_issue("...\n\nCloses #265")
    none = c.exactly_one_linked_issue("no link here")
    assert not none and "no closing issue" in none.reasons[0]
    two = c.exactly_one_linked_issue("Closes #1 and Fixes #2")
    assert not two and "2 issues" in two.reasons[0]


def test_c0_present_blocks_on_unresolved_fork():
    c = _load()
    assert c.c0_present(["type/task", "area/ci"])
    blocked = c.c0_present(["status/blocked"])
    assert not blocked and "status/blocked" in blocked.reasons[0]
    assert not c.c0_present(["status/needs-decision"])


def test_code_change_requires_test():
    c = _load()
    assert c.code_change_requires_test(["scripts/devloop/coupling.py",
                                        "ack/tests/test_devloop_coupling.py"])     # source + test
    red = c.code_change_requires_test(["scripts/devloop/coupling.py"])                  # source, no test
    assert not red and "without a test" in red.reasons[0]
    assert c.code_change_requires_test(["docs/x.md", "README.md"])                      # no source => pass


def test_capability_change_requires_docs():
    c = _load()
    caps = ["*/ack/sdk.py"]
    docs = ["*.md"]
    red = c.capability_change_requires_docs(["ack/sdk.py"], capability_patterns=caps, doc_patterns=docs)
    assert not red and "without docs" in red.reasons[0]
    assert c.capability_change_requires_docs(["ack/sdk.py", "docs/status.md"],
                                             capability_patterns=caps, doc_patterns=docs)
    assert c.capability_change_requires_docs(["ack/other.py"],                     # no capability touched
                                             capability_patterns=caps, doc_patterns=docs)
