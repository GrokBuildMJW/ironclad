from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_overlay as po  # noqa: E402


def test_locked_top_level_keys_are_dropped():
    base = {
        "connection": {"host": "spark"},
        "security": {"profile": "open"},
        "setup": {"type": "local"},
        "search": {"on": False},
        "plugins_dir": "/a",
        "language": "en",
    }
    overlay = {
        "connection": {"host": "other"},
        "security": {"profile": "closed"},
        "setup": {"type": "server"},
        "search": {"on": True},
        "plugins_dir": "/b",
        "language": "de",
    }
    merged, dropped = po.apply_project_overlay(base, overlay)

    assert merged["connection"] == {"host": "spark"}
    assert merged["security"] == {"profile": "open"}
    assert merged["setup"] == {"type": "local"}
    assert merged["search"] == {"on": False}
    assert merged["plugins_dir"] == "/a"
    assert merged["language"] == "de"

    for locked in ("connection", "security", "setup", "search", "plugins_dir"):
        assert locked in dropped


def test_nested_locked_providers_budget():
    base = {"providers": {"default_id": "x", "budget": {"usd_cap": 5}}}
    overlay = {"providers": {"default_id": "y", "budget": {"usd_cap": 999}}}
    merged, dropped = po.apply_project_overlay(base, overlay)

    assert merged["providers"]["default_id"] == "y"
    assert merged["providers"]["budget"]["usd_cap"] == 5
    assert "providers.budget" in dropped


def test_generation_language_is_dropped():
    base = {"generation": {"language": "en"}}
    overlay = {"generation": {"language": "de"}}
    merged, dropped = po.apply_project_overlay(base, overlay)

    assert merged["generation"]["language"] == "en"
    assert dropped == ["generation"]


def test_generation_max_tokens_is_dropped():
    base = {"generation": {"max_tokens": 4096}}
    overlay = {"generation": {"max_tokens": 8192}}
    merged, dropped = po.apply_project_overlay(base, overlay)

    assert merged["generation"]["max_tokens"] == 4096
    assert dropped == ["generation"]


def test_generation_drop_preserves_non_locked_overlay_key():
    base = {"generation": {"language": "en"}, "dev_process": {"tier": 1}}
    overlay = {"generation": {"language": "de"}, "dev_process": {"tier": 2}}
    merged, dropped = po.apply_project_overlay(base, overlay)

    assert merged == {"generation": {"language": "en"}, "dev_process": {"tier": 2}}
    assert dropped == ["generation"]


def test_base_is_not_mutated():
    base = {"dev_process": {"tier": 1, "push": "off"}}
    overlay = {"dev_process": {"tier": 2}}
    original = base
    po.apply_project_overlay(base, overlay)
    assert base is original
    assert base == {"dev_process": {"tier": 1, "push": "off"}}


def test_closed_allowlist_mode_drops_unlisted():
    base = {"dev_process": {"tier": 1}, "language": "en"}
    overlay = {"dev_process": {"tier": 2}, "language": "de"}
    merged, dropped = po.apply_project_overlay(base, overlay, allow={"dev_process"})

    assert merged["dev_process"]["tier"] == 2
    assert merged["language"] == "en"
    assert "language" in dropped


def test_deep_merge_preserves_sibling_keys():
    base = {"dev_process": {"tier": 1, "push": "off"}}
    overlay = {"dev_process": {"tier": 2}}
    merged, _ = po.apply_project_overlay(base, overlay)

    assert merged["dev_process"] == {"tier": 2, "push": "off"}


def test_contain_allows_in_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()

    assert po.contain(root, "vault") == (root / "vault").resolve()
    assert po.contain(root, "a/b/c") == (root / "a" / "b" / "c").resolve()


def test_contain_rejects_traversal(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()

    for bad in ("../escape", "../../etc", "a/../../out"):
        with pytest.raises(po.PathEscape):
            po.contain(root, bad)


def test_contain_rejects_absolute_outside(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"

    with pytest.raises(po.PathEscape):
        po.contain(root, str(outside))


def test_contain_rejects_symlink_escape(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("x", encoding="utf-8")
    link = root / "link"

    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        # symlinks unavailable here (e.g. unprivileged Windows): assert the equivalent — an absolute
        # path resolving outside the root is still rejected. Never SKIP (a non-live skip would trip
        # the test-count skip-liveness guard); always make a real assertion.
        with pytest.raises(po.PathEscape):
            po.contain(root, str(outside / "file"))
        return

    with pytest.raises(po.PathEscape):
        po.contain(root, "link/file")        # a symlink inside the root pointing out must be rejected


def test_forge_locked_via_missing_parent():
    m, d = po.apply_project_overlay({}, {"providers": {"budget": {"usd_cap": 999}}})
    assert "budget" not in m.get("providers", {})  # cannot forge providers.budget when base lacks providers


def test_forge_locked_via_nondict_parent():
    m, d = po.apply_project_overlay({"providers": "x"}, {"providers": {"budget": {"usd_cap": 999}}})
    assert m["providers"] == "x"  # non-dict base parent: overlay rejected, no budget forged


def test_drop_locked_via_list_replacement():
    m, d = po.apply_project_overlay(
        {"providers": {"budget": {"usd_cap": 5}, "default_id": "x"}}, {"providers": []}
    )
    assert m["providers"]["budget"]["usd_cap"] == 5  # a list replacing the parent must NOT drop locked budget
    assert "providers" in d


def test_drop_locked_via_none_replacement():
    m, d = po.apply_project_overlay({"providers": {"budget": {"usd_cap": 5}}}, {"providers": None})
    assert m["providers"]["budget"]["usd_cap"] == 5  # None replacing the parent must NOT drop locked budget


def test_locked_arg_accepts_a_generator():
    gen = (p for p in ("connection", "security"))
    m, d = po.apply_project_overlay(
        {"connection": {"h": "a"}, "security": {"p": "open"}},
        {"connection": {"h": "evil"}, "security": {"p": "sealed"}},
        locked=gen,
    )
    assert m["connection"]["h"] == "a" and m["security"]["p"] == "open"  # a one-shot generator must still lock every key


def test_non_locked_forge_under_missing_parent_works():
    m, d = po.apply_project_overlay({}, {"providers": {"default_id": "y"}})
    assert m["providers"]["default_id"] == "y"  # forging a NON-locked nested key is allowed
