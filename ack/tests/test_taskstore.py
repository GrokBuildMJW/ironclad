"""TaskStore — the deterministic task lifecycle + topic dedup (core engine state).

Exercised directly (no model): create → transition → list, monotonic IDs, required-
field validation, and the same-topic duplicate guard that the stage_handover gate
relies on.
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
import pytest  # noqa: E402

VALID = {"type": "feature", "priority": "high", "title": "Add rate limiting",
         "description": "Throttle the public API per client."}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return gx10.TaskStore(root=".")


def test_create_lands_in_pending(store):
    t = store.create(dict(VALID))
    assert t["status"] == "pending"
    assert t["id"].startswith(gx10.TASK_PREFIX + "-")
    assert store.get(t["id"])["title"] == "Add rate limiting"
    assert [x["id"] for x in store.list("pending")] == [t["id"]]


def test_ids_are_monotonic(store):
    a = store.create(dict(VALID))
    b = store.create({**VALID, "title": "Something else entirely",
                      "description": "unrelated work"})
    na = int(a["id"].split("-")[1])
    nb = int(b["id"].split("-")[1])
    assert nb == na + 1


def test_transition_moves_status(store):
    t = store.create(dict(VALID))
    store.transition(t["id"], "in_progress")
    assert store.get(t["id"])["status"] == "in_progress"
    assert store.list("pending") == []
    store.transition(t["id"], "done")
    assert store.get(t["id"])["status"] == "done"


def test_invalid_status_rejected(store):
    t = store.create(dict(VALID))
    with pytest.raises(ValueError):
        store.transition(t["id"], "bogus")


def test_missing_required_fields_rejected(store):
    with pytest.raises(ValueError):
        store.create({"type": "feature", "title": "no priority/description"})


def test_same_topic_duplicate_guarded(store):
    store.create(dict(VALID))
    with pytest.raises(gx10.DuplicateTaskError) as exc_info:
        store.create({**VALID, "title": "Add rate limiting to the API",
                      "description": "Throttle the public API per client, per key."})
    assert exc_info.value.exact is False


def test_force_overrides_fuzzy_duplicate(store):
    store.create(dict(VALID))
    forced = store.create({**VALID, "title": "Add rate limiting to the API",
                           "description": "Throttle the public API per client, per key."},
                          force=True)
    assert forced["status"] == "pending"
    assert len(store.list("pending")) == 2


def test_force_does_not_override_exact_title_duplicate(store):
    existing = store.create(dict(VALID))
    with pytest.raises(gx10.DuplicateTaskError) as unforced:
        store.create(dict(VALID))
    assert unforced.value.existing_id == existing["id"]
    assert unforced.value.exact is True

    with pytest.raises(gx10.DuplicateTaskError) as forced:
        store.create(dict(VALID), force=True)
    assert forced.value.existing_id == existing["id"]
    assert forced.value.exact is True
    assert [task["id"] for task in store.list("pending")] == [existing["id"]]


def test_unknown_id_get_is_none(store):
    assert store.get("KGC-999") is None
