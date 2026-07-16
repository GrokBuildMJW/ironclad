"""Project Registry (engine/project_registry.py, #621 / ADR-0011 AD-6) - the installation-global SSOT.

Pins the durable contract: atomic round-trip, minted-unique mem_ns, the cross-process FileLock (with
contention timeout only), the ADR-0007 reconciler (re-mint a duplicate, quarantine a missing root,
rebuild-from-disk), the implicit `default` project, and per-slug legacy vault migration. Offline,
filesystem-only (home = tmp_path), no engine globals touched (that is S3)."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_registry as reg  # noqa: E402
from project_registry import FileLock, Registry  # noqa: E402


def _r(tmp_path) -> Registry:
    return Registry(home=tmp_path / "home")


# ---- minting -------------------------------------------------------------------------------------
def test_mint_mem_ns_is_64bit_and_unique():
    ns = [reg.mint_mem_ns() for _ in range(2000)]
    assert all(len(n) == 16 and int(n, 16) >= 0 for n in ns)   # 16 hex = 64 bits
    assert len(set(ns)) == len(ns)                              # collision-free in practice


def test_ironclad_home_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GX10_HOME", str(tmp_path / "custom"))
    assert reg.ironclad_home() == tmp_path / "custom"
    monkeypatch.delenv("GX10_HOME", raising=False)
    assert reg.ironclad_home().name in ("ironclad", ".ironclad")  # per-user default


# ---- register / round-trip ----------------------------------------------------------------------
def test_register_round_trips_atomically(tmp_path):
    r = _r(tmp_path)
    proj = r.register("alpha", tmp_path / "alpha", created="2026-06-27")
    assert proj.id == "alpha" and proj.slug == "alpha" and proj.mem_ns and proj.active_track == "main"
    again = Registry(home=tmp_path / "home").get("alpha")      # a FRESH Registry sees the persisted state
    assert again is not None and again.mem_ns == proj.mem_ns
    assert again.root == str((tmp_path / "alpha").resolve())
    data = json.loads((tmp_path / "home" / "registry.json").read_text(encoding="utf-8"))
    assert data["active"] == "alpha" and "alpha" in data["projects"]


def test_register_mints_unique_mem_ns_per_project(tmp_path):
    r = _r(tmp_path)
    assert r.register("a", tmp_path / "a").mem_ns != r.register("b", tmp_path / "b").mem_ns


def test_register_rejects_duplicate_id(tmp_path):
    r = _r(tmp_path)
    r.register("dup", tmp_path / "dup")
    with pytest.raises(ValueError):
        r.register("dup", tmp_path / "dup2")


def test_register_rejects_duplicate_root(tmp_path):
    r = _r(tmp_path)
    shared = tmp_path / "shared"
    r.register("a", shared)
    with pytest.raises(ValueError):
        r.register("b", shared)


def test_register_rejects_unsafe_id_and_slug(tmp_path):
    r = _r(tmp_path)
    with pytest.raises(ValueError):
        r.register("ok", tmp_path / "x", project_id="../evil")
    with pytest.raises(ValueError):
        r.register("a/b", tmp_path / "x2")


def test_first_register_becomes_active_then_explicit_switch(tmp_path):
    r = _r(tmp_path)
    r.register("one", tmp_path / "one")
    r.register("two", tmp_path / "two")
    assert r.active().id == "one"                               # first wins by default
    r.set_active("two")
    assert r.active().id == "two"
    with pytest.raises(KeyError):
        r.set_active("ghost")


def test_remove_reassigns_active(tmp_path):
    r = _r(tmp_path)
    r.register("one", tmp_path / "one")
    r.register("two", tmp_path / "two")
    r.set_active("one")
    r.remove("one")
    assert r.get("one") is None and r.active().id == "two"


# ---- default project -----------------------------------------------------------------------------
def test_ensure_default_is_idempotent(tmp_path):
    r = _r(tmp_path)
    d1 = r.ensure_default(tmp_path / "wd")
    d2 = r.ensure_default(tmp_path / "wd")
    assert d1.id == reg.DEFAULT_PROJECT_ID and d2.mem_ns == d1.mem_ns
    assert len([p for p in r.list() if p.id == reg.DEFAULT_PROJECT_ID]) == 1


def test_load_corrupt_registry_is_quarantined_not_destroyed(tmp_path):
    r = _r(tmp_path)
    project = r.register("alpha", tmp_path / "a")
    corrupt = r.path.read_bytes()[:-15]
    r.path.write_bytes(corrupt)

    assert r.load() == {"projects": {}, "active": None}
    quarantined = list(r.home.glob("registry.json.corrupt.*"))
    assert len(quarantined) == 1
    preserved = quarantined[0].read_bytes()
    assert preserved == corrupt
    assert b"alpha" in preserved and project.mem_ns.encode() in preserved

    r.ensure_default(tmp_path / "wd")
    fresh = json.loads(r.path.read_text(encoding="utf-8"))
    assert reg.DEFAULT_PROJECT_ID in fresh["projects"]


def test_load_missing_registry_is_first_run_no_quarantine(tmp_path):
    r = _r(tmp_path)
    assert r.load() == {"projects": {}, "active": None}
    assert list(r.home.glob("registry.json.corrupt.*")) == []


def test_load_non_dict_registry_is_quarantined(tmp_path):
    # Valid JSON whose top level is not an object is corruption, not a first run — it must be quarantined,
    # not silently reduced to empty and overwritten.
    r = _r(tmp_path)
    r.register("alpha", tmp_path / "a")
    r.path.write_bytes(b"[]")

    assert r.load() == {"projects": {}, "active": None}
    assert len(list(r.home.glob("registry.json.corrupt.*"))) == 1


def test_load_corrupt_registry_fails_closed_when_unmovable(tmp_path, monkeypatch):
    # If the corrupt file cannot be moved aside but is still present (e.g. a Windows lock), load must FAIL
    # rather than return empty — otherwise a subsequent _save would overwrite the unrecovered bytes.
    r = _r(tmp_path)
    project = r.register("alpha", tmp_path / "a")
    r.path.write_bytes(r.path.read_bytes()[:-15])

    def _boom(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(reg.os, "replace", _boom)

    with pytest.raises(OSError):
        r.load()
    assert r.path.exists()                                    # corrupt bytes preserved, not clobbered
    assert b"alpha" in r.path.read_bytes() and project.mem_ns.encode() in r.path.read_bytes()
    assert list(r.home.glob("registry.json.corrupt.*")) == []


def test_concurrent_ensure_default_converges_to_one(tmp_path):
    r = _r(tmp_path)
    wd = tmp_path / "wd"
    results = []
    lock = threading.Lock()

    def worker():
        proj = r.ensure_default(wd)
        with lock:
            results.append(proj)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    default_projects = [p for p in r.list() if p.id == reg.DEFAULT_PROJECT_ID]
    assert len(default_projects) == 1
    assert {p.mem_ns for p in results} == {default_projects[0].mem_ns}


# ---- FileLock ------------------------------------------------------------------------------------
def test_filelock_is_exclusive_and_times_out(tmp_path):
    lp = tmp_path / "x.lock"
    held = FileLock(lp).acquire()
    try:
        with pytest.raises(TimeoutError):
            FileLock(lp, timeout_s=0.2).acquire()
    finally:
        held.release()
    FileLock(lp, timeout_s=0.2).acquire().release()            # released -> acquirable again


def test_project_lock_rejects_traversal_id(tmp_path):
    r = _r(tmp_path)
    with pytest.raises(ValueError):
        r.project_lock("../evil")
    lock = r.project_lock("good")
    assert isinstance(lock, FileLock)
    assert lock.path == r.home / "locks" / "good.lock"


def test_filelock_is_cross_process_exclusive(tmp_path):
    """A lock held by another PROCESS blocks acquisition here, and frees when that process releases."""
    import subprocess, sys, time
    lp = tmp_path / "cp.lock"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    child = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[4])\n"
        "from project_registry import FileLock\n"
        "lk = FileLock(Path(sys.argv[1])).acquire()\n"
        "Path(sys.argv[2]).write_text('1')\n"
        "rel = Path(sys.argv[3])\n"
        "while not rel.exists():\n"
        "    time.sleep(0.01)\n"
        "lk.release()\n"
    )
    p = subprocess.Popen([sys.executable, "-c", child, str(lp), str(ready), str(release), str(_ENGINE)])
    try:
        for _ in range(500):                       # wait until the child holds the lock
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "child failed to acquire the lock"
        with pytest.raises(TimeoutError):          # we cannot acquire while the child holds it
            FileLock(lp, timeout_s=0.4).acquire()
    finally:
        release.write_text("1")                    # tell the child to release
        p.wait(timeout=10)
    FileLock(lp, timeout_s=2).acquire().release()  # acquirable once the child has released


# ---- reconcile (ADR-0007 self-heal) --------------------------------------------------------------
def test_reconcile_remints_a_duplicate_mem_ns(tmp_path):
    r = _r(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    r.register("a", tmp_path / "a")
    r.register("b", tmp_path / "b")
    shared = r.get("a").mem_ns
    raw = json.loads(r.path.read_text(encoding="utf-8"))        # hand-force a collision (a copied dir)
    raw["projects"]["b"]["mem_ns"] = shared
    r.path.write_text(json.dumps(raw), encoding="utf-8")
    rep = r.reconcile()
    assert "b" in rep["reminted"]
    assert r.get("a").mem_ns != r.get("b").mem_ns               # no shared partition after heal


def test_reconcile_quarantines_a_missing_root(tmp_path):
    r = _r(tmp_path)
    r.register("gone", tmp_path / "gone")                       # never created on disk
    assert "gone" in r.reconcile()["quarantined"]


def test_reconcile_remints_low_entropy_mem_ns(tmp_path):
    r = _r(tmp_path)
    (tmp_path / "low").mkdir()
    r.register("low", tmp_path / "low")
    raw = json.loads(r.path.read_text(encoding="utf-8"))
    raw["projects"]["low"]["mem_ns"] = "x"
    r.path.write_text(json.dumps(raw), encoding="utf-8")
    rep = r.reconcile()
    assert "low" in rep["reminted"]
    assert reg.valid_mem_ns(r.get("low").mem_ns)


def test_load_tolerates_a_malformed_entry(tmp_path):
    r = _r(tmp_path)
    r.register("good", tmp_path / "good")
    data = json.loads(r.path.read_text(encoding="utf-8"))
    data["projects"]["bad"] = {
        "id": "bad",
        "slug": "bad",
        "mem_ns": "1234567890abcdef",
        # missing "root"
    }
    r.path.write_text(json.dumps(data), encoding="utf-8")
    loaded = r.load()["projects"]
    assert "good" in loaded and "bad" not in loaded
    assert r.get("bad") is None


def test_reconcile_rebuilds_unregistered_disk_roots(tmp_path):
    r = _r(tmp_path)
    proj_dir = tmp_path / "found"
    proj_dir.mkdir()
    rep = r.reconcile(disk_roots=[proj_dir])
    assert "found" in rep["rebuilt"]
    assert r.get("found").root == str(proj_dir.resolve())


def test_reconcile_rebuild_skips_files_and_special_dirs(tmp_path):
    r = _r(tmp_path)
    good = tmp_path / "good"
    afile = tmp_path / "afile"
    hidden = tmp_path / ".hidden"
    archive = tmp_path / "_archive"
    good.mkdir()
    afile.write_text("x", encoding="utf-8")
    hidden.mkdir()
    archive.mkdir()
    rep = r.reconcile(disk_roots=[good, afile, hidden, archive])
    assert rep["rebuilt"] == ["good"]


# ---- legacy migration (per-slug, NOT one default) ------------------------------------------------
def test_migrate_legacy_vaults_one_project_per_slug(tmp_path):
    vault = tmp_path / "vault"
    for slug in ("order-service", "billing"):
        d = vault / slug
        d.mkdir(parents=True)
        (d / "meta.md").write_text("type: software\ntitle: x\n", encoding="utf-8")
    (vault / "_archive").mkdir()                                # underscore dir is skipped
    (vault / "loose.md").write_text("x", encoding="utf-8")     # non-dir is skipped
    r = _r(tmp_path)
    migrated = r.migrate_legacy_vaults(vault)
    assert sorted(migrated) == ["billing", "order-service"]     # one project PER slug
    assert {p.id for p in r.list()} == {"billing", "order-service"}
    assert len({p.mem_ns for p in r.list()}) == 2               # distinct partitions


# ---- regression: project isolation fixes (default re-point / repair / active preservation) ----------
def test_ensure_default_keeps_stored_root(tmp_path):
    r = _r(tmp_path)
    a = tmp_path / "wdA"; a.mkdir()
    b = tmp_path / "wdB"; b.mkdir()
    d1 = r.ensure_default(a)
    assert d1.root == str(a.resolve())
    ns1 = d1.mem_ns
    d2 = r.ensure_default(b)
    assert d2.root == str(a.resolve())          # an existing default's root is NOT re-pointed
    assert d2.mem_ns == ns1


def test_ensure_default_repairs_missing_active(tmp_path):
    r = _r(tmp_path)
    b = tmp_path / "wd"; b.mkdir()
    r.ensure_default(b)
    st = r.load(); st["active"] = None; r._save(st)
    assert r.active() is None
    r.ensure_default(b)
    assert r.active().id == "default"


def test_ensure_default_keeps_registered_active(tmp_path):
    r = _r(tmp_path)
    b = tmp_path / "wd"; b.mkdir()
    r.ensure_default(b)
    r.register("acme", b / "acme", make_active=True)
    assert r.active().id == "acme"
    r.ensure_default(b)
    assert r.active().id == "acme"             # must not steal active from a registered project
