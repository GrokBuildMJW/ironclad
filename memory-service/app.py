"""Optional long-term memory API (Mem0 0.1.118) — the backend behind Ironclad's
memory hooks.

LLM (fact extraction) = any OpenAI-compatible endpoint (e.g. the same vLLM the
orchestrator uses), external. Embeddings = BGE-M3 (local, 1024d). Vector store =
Qdrant. Graph store = Neo4j.

Scoping: every agent/project shares one instance via user_id/agent_id/run_id.
"Learning": add(infer=True) extracts facts, detects contradictions and UPDATEs
existing memories (ADD/UPDATE/DELETE) instead of only appending.

Secret-free: all connection details come from env (with localhost-style defaults);
NEO4J_PASSWORD is required (no default). Pair with Ironclad by pointing the engine at
this service: GX10_MEMORY_URL=http://<host>:8800.
"""
import os
import time
import json
import logging
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from mem0 import Memory

import scope_guard  # pure AD-4 isolation guards (stdlib-only, unit-tested offline)
from reflect_policy import reflect_decision  # pure threshold-fire policy (MEMSVC-1, stdlib-only, offline-tested)
import curate  # pure curated-global helpers (#634, stdlib-only, offline-tested)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mem0-api")

# --- Robustness patch for Mem0's graph extraction --------------------------
# MemoryGraph._remove_spaces_from_entities blindly assumes
# item["source"/"relationship"/"destination"]. Local LLMs sometimes return
# relations without those keys -> KeyError -> HTTP 500 -> the whole graph write is
# lost. Defensive version: skip malformed relations instead of crashing.
import mem0.memory.graph_memory as _gm  # noqa: E402


def _safe_remove_spaces(self, entity_list):
    cleaned = []
    for item in entity_list:
        if isinstance(item, dict) and item.get("source") and item.get("relationship") and item.get("destination"):
            item["source"] = str(item["source"]).lower().replace(" ", "_")
            item["relationship"] = _gm.sanitize_relationship_for_cypher(
                str(item["relationship"]).lower().replace(" ", "_")
            )
            item["destination"] = str(item["destination"]).lower().replace(" ", "_")
            cleaned.append(item)
    return cleaned


_gm.MemoryGraph._remove_spaces_from_entities = _safe_remove_spaces
log.info("patch active: _remove_spaces_from_entities (malformed relations skipped)")
# ---------------------------------------------------------------------------

CONFIG = {
    "llm": {
        "provider": "vllm",
        "config": {
            "model": os.environ.get("MEM0_LLM_MODEL", "qwen3.6-35b"),
            "vllm_base_url": os.environ.get("MEM0_LLM_BASE_URL", "http://localhost:8000/v1"),
            "api_key": os.environ.get("MEM0_LLM_API_KEY", "not-needed"),
            "temperature": 0.1,
            "max_tokens": 2000,
        },
    },
    "embedder": {
        "provider": "huggingface",
        "config": {"model": "BAAI/bge-m3", "embedding_dims": 1024},
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "agent_memory",
            "host": os.environ.get("QDRANT_HOST", "qdrant"),
            "port": int(os.environ.get("QDRANT_PORT", "6333")),
            "embedding_model_dims": 1024,
            "on_disk": True,
        },
    },
    "graph_store": {
        "provider": "neo4j",
        "config": {
            "url": os.environ.get("NEO4J_URL", "bolt://neo4j:7687"),
            "username": os.environ.get("NEO4J_USER", "neo4j"),
            "password": os.environ["NEO4J_PASSWORD"],
        },
    },
}


def _init_with_retry(cfg: dict, label: str, retries: int = 30, delay: int = 5) -> Memory:
    """Wait for Qdrant/Neo4j/the LLM endpoint instead of crashing hard on startup."""
    last = None
    for i in range(retries):
        try:
            m = Memory.from_config(cfg)
            log.info("Mem0 initialised [%s] (attempt %d)", label, i + 1)
            return m
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("Mem0 init [%s] attempt %d/%d failed: %s", label, i + 1, retries, exc)
            time.sleep(delay)
    raise RuntimeError(f"Mem0 init [{label}] ultimately failed: {last}")


# Full instance: vector + graph (runtime enrichment).
mem = _init_with_retry(CONFIG, "full")

# Vector-only instance (NO graph_store): for fast bulk import. Mem0 would otherwise run
# graph extraction (LLM!) even at infer=False -> slow. Both share the same Qdrant
# collection.
CONFIG_VEC = {k: v for k, v in CONFIG.items() if k != "graph_store"}
mem_vec = _init_with_retry(CONFIG_VEC, "vector-only")

# Curated-global tier (#634 / ADR-0011 AD-9): a SEPARATE physical Qdrant collection of operator-promoted,
# redacted, cross-project knowledge. Mem0 0.1.118 binds collection_name at construction (no per-call
# override), so the curated tier needs its OWN instance. Vector-only (no graph): curated knowledge is
# retrieval-only, and a separate graph namespace on Community Neo4j is single-DB anyway. Being a separate
# COLLECTION, it never appears in /scopes (which scrolls agent_memory) nor in the registry-keyed orphan GC.
CURATED_COLLECTION = os.environ.get("CURATED_COLLECTION", "curated_global")
CONFIG_CURATED = {k: v for k, v in CONFIG.items() if k != "graph_store"}
CONFIG_CURATED["vector_store"] = {
    **CONFIG["vector_store"],
    "config": {**CONFIG["vector_store"]["config"], "collection_name": CURATED_COLLECTION},
}
mem_curated = _init_with_retry(CONFIG_CURATED, "curated")

app = FastAPI(title="Agent Memory (Mem0)", version="1.0")

# === Reflection (PER-mem_ns, threshold-triggered) ==========================
# Counts learning writes (infer=True) PER mem_ns partition; once REFLECT_EVERY accumulate for a partition it
# fires a background graph-hygiene run for THAT partition (one per partition at a time). #634: per-mem_ns
# (was a single GLOBAL counter+lock) + a Cypher SCOPED to the firing partition + a configurable merge.
REFLECT_EVERY = int(os.environ.get("REFLECT_EVERY_N_WRITES", "50"))
REFLECT_STATE = os.environ.get("REFLECT_STATE_PATH", "/hf-cache/reflect_state.json")
# 'discard' (the historical default, byte-identical) | 'combine' (#634 non-lossy: conflicting scalar props
# become arrays). 'combine' is OPT-IN via the env so downstream graph reads can be verified live before it
# becomes the default (the #634 live-check exercises it).
REFLECT_MERGE_PROPS = os.environ.get("REFLECT_MERGE_PROPS", "discard")
_WARM_URL = os.environ.get("MEM0_WARM_URL", "").strip()   # optional Valkey for an atomic, MULTI-worker counter+lock

_state_lock = threading.Lock()
_scope_locks_guard = threading.Lock()
_scope_locks: dict = {}            # per-mem_ns in-process reflection lock (the single-worker-correct fallback)
_redis_client = None
_redis_init = False


def _scope_lock(ns: str) -> threading.Lock:
    with _scope_locks_guard:
        return _scope_locks.setdefault(ns, threading.Lock())


def _redis():
    """A Valkey client if MEM0_WARM_URL is set + reachable (init once, lazily), else None. **Fail-soft**: a
    Valkey outage NEVER breaks /add — the per-mem_ns counter+lock fall back to the file + in-process
    mechanism, which is correct at the single uvicorn worker this image runs. The interprocess Valkey lock is
    the prerequisite for running >1 worker."""
    global _redis_client, _redis_init
    if not _WARM_URL:
        return None
    if not _redis_init:
        _redis_init = True
        try:
            import redis
            c = redis.from_url(_WARM_URL, socket_connect_timeout=2, socket_timeout=2)
            c.ping()
            _redis_client = c
            log.info("reflect: Valkey counter+lock active (%s)", _WARM_URL.split("@")[-1])
        except Exception as exc:  # noqa: BLE001
            log.warning("reflect: Valkey unreachable (%s) — per-scope file/in-proc fallback", exc)
            _redis_client = None
    return _redis_client


def _load_state() -> dict:
    try:
        with open(REFLECT_STATE) as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("scopes", {})        # {mem_ns: writes_since} — per-partition counters (#634)
    return st


def _save_state(s: dict) -> None:
    try:
        with open(REFLECT_STATE, "w") as f:
            json.dump(s, f)
    except Exception as exc:  # noqa: BLE001
        log.warning("reflect state save: %s", exc)


def _reflection_running(ns: str) -> bool:
    r = _redis()
    if r is not None:
        try:
            return bool(r.exists(f"reflect:lock:{ns}"))
        except Exception:  # noqa: BLE001
            pass
    return _scope_lock(ns).locked()


def _account_write(ns: str) -> bool:
    """Count one learning write for partition *ns*; return True iff a reflection should FIRE (threshold AND
    none running for ns). Valkey INCR (atomic, multi-worker) when available, else the file counter under
    _state_lock (atomic at one worker) reusing the offline-tested reflect_decision. Consumes on fire; while a
    run is in progress the count accumulates toward the next cycle (no undercount, no bail-thread churn —
    MEMSVC-1, now per-partition)."""
    running = _reflection_running(ns)
    r = _redis()
    if r is not None:
        try:
            n = int(r.incr(f"reflect:writes:{ns}"))
            if REFLECT_EVERY >= 1 and n >= REFLECT_EVERY and not running:
                r.set(f"reflect:writes:{ns}", 0)        # consume
                return True
            return False
        except Exception:  # noqa: BLE001
            pass                                        # fall through to the file counter
    with _state_lock:
        st = _load_state()
        st["scopes"][ns], fire = reflect_decision(st["scopes"].get(ns, 0), REFLECT_EVERY, running)
        _save_state(st)
    return fire


def _acquire_scope_lock(ns: str):
    """Acquire the per-scope reflection lock — interprocess via Valkey (SET NX EX) when available, else the
    in-process lock (one-worker-correct). Returns a release callable, or None if already held (skip)."""
    r = _redis()
    if r is not None:
        try:
            if r.set(f"reflect:lock:{ns}", "1", nx=True, ex=1800):
                def _rel():
                    try:
                        r.delete(f"reflect:lock:{ns}")
                    except Exception:  # noqa: BLE001
                        pass
                return _rel
            return None
        except Exception:  # noqa: BLE001
            pass                                        # fall through to the in-process lock
    lk = _scope_lock(ns)
    return lk.release if lk.acquire(blocking=False) else None


def _graph_hygiene(mem_ns: "str | None" = None) -> dict:
    """Merge duplicate entities of the same name+scope (relations preserved via mergeRels). #634: when
    *mem_ns* is given the merge is SCOPED to that partition (``n.agent_id = $scope_ns``) — bounded + isolated;
    None = the operator's full-store sweep. The merge strategy is REFLECT_MERGE_PROPS ('discard' default,
    'combine' = non-lossy)."""
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(
        os.environ.get("NEO4J_URL", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    props = "combine" if REFLECT_MERGE_PROPS == "combine" else "discard"
    scope = "AND n.agent_id = $scope_ns " if mem_ns else ""
    try:
        with drv.session() as s:
            nb = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            rb = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            merged = s.run(
                "MATCH (n) WHERE n.name IS NOT NULL " + scope +
                "WITH n.name AS nm, n.user_id AS uid, n.agent_id AS aid, collect(n) AS ns "
                "WHERE size(ns) > 1 "
                "CALL apoc.refactor.mergeNodes(ns, {properties:$props, mergeRels:true}) YIELD node "
                "RETURN count(*) AS m",
                {"props": props, **({"scope_ns": mem_ns} if mem_ns else {})},
            ).single()["m"]
            na = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            ra = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        return {"nodes_before": nb, "nodes_after": na, "rels_before": rb, "rels_after": ra,
                "duplicate_groups_merged": merged, "scope": mem_ns or "*", "merge": props}
    finally:
        drv.close()


def _run_reflection(reason: str, mem_ns: str = "*") -> None:
    release = _acquire_scope_lock(mem_ns)
    if release is None:
        log.info("reflection already running for %s — skip (%s)", mem_ns, reason)
        return
    try:
        t0 = time.time()
        log.info("reflection START scope=%s (%s)", mem_ns, reason)
        summary = {"reason": reason, "scope": mem_ns, "error": None}
        try:
            summary["graph"] = _graph_hygiene(None if mem_ns == "*" else mem_ns)
        except Exception as exc:  # noqa: BLE001
            summary["error"] = str(exc)
            log.warning("reflection error: %s", exc)
        summary["seconds"] = round(time.time() - t0, 1)
        with _state_lock:
            st = _load_state()
            st["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            st["last_summary"] = summary
            _save_state(st)
        log.info("reflection DONE: %s", summary)
    finally:
        release()


def _maybe_reflect(agent_id: "str | None") -> None:
    """Account one learning write for the active partition + fire its reflection on the threshold (#634
    per-mem_ns). An unscoped write never reflects (scope_guard already requires agent_id on /add)."""
    ns = agent_id.strip() if (isinstance(agent_id, str) and agent_id.strip()) else None
    if ns is None:
        return
    if _account_write(ns):
        threading.Thread(target=_run_reflection, args=(f"threshold/{REFLECT_EVERY}", ns), daemon=True).start()


@app.post("/reflect")
def reflect_now(agent_id: "str | None" = None):
    """Manual trigger (background). With *agent_id* the hygiene is scoped to that partition; without it the
    operator's full-store sweep (``*``)."""
    ns = agent_id.strip() if (isinstance(agent_id, str) and agent_id.strip()) else "*"
    threading.Thread(target=_run_reflection, args=("manual", ns), daemon=True).start()
    return {"status": f"reflection started (background, scope={ns})"}


@app.get("/reflect/status")
def reflect_status():
    st = _load_state()
    st["reflect_every_n_writes"] = REFLECT_EVERY
    st["merge_props"] = REFLECT_MERGE_PROPS
    st["valkey"] = _redis() is not None
    return st
# ============================================================================


class AddReq(BaseModel):
    messages: list
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    metadata: dict | None = None
    infer: bool = True   # False = raw store (bulk import, no LLM); True = fact extraction + graph


class SearchReq(BaseModel):
    query: str
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    limit: int = 10
    graph: bool = True   # False = vector only (mem_vec, no graph LLM) → fast, no read timeout
    include_curated: bool = False   # #634: opt-in fan-in of the curated-global tier (PROJECT-WINS); default off = byte-identical


class DeleteReq(BaseModel):
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/add")
def add(r: AddReq):
    if (err := scope_guard.require_scope(r.agent_id, r.run_id)):
        raise HTTPException(status_code=400, detail=err)
    res = mem.add(
        r.messages, user_id=r.user_id, agent_id=r.agent_id,
        run_id=r.run_id, metadata=r.metadata, infer=r.infer,
    )
    if r.infer:
        _maybe_reflect(r.agent_id)   # #634: account the write for THIS partition (per-mem_ns reflection)
    return res


@app.post("/add_bulk")
def add_bulk(r: AddReq):
    """Bulk: vector only (no graph, no LLM) → fast. infer always False."""
    if (err := scope_guard.require_scope(r.agent_id, r.run_id)):
        raise HTTPException(status_code=400, detail=err)
    return mem_vec.add(
        r.messages, user_id=r.user_id, agent_id=r.agent_id,
        run_id=r.run_id, metadata=r.metadata, infer=False,
    )


@app.post("/search")
def search(r: SearchReq):
    if (err := scope_guard.require_scope(r.agent_id, r.run_id)):
        raise HTTPException(status_code=400, detail=err)
    # graph=False → mem_vec (pure vector search). The graph search of `mem` extracts
    # query entities via the LLM and occasionally tripped the client read timeout. For
    # retrieval/dedup the vector path is enough.
    inst = mem if r.graph else mem_vec
    proj = inst.search(
        r.query, user_id=r.user_id, agent_id=r.agent_id,
        run_id=r.run_id, limit=r.limit,
    )
    # #634 / AD-9: fan in operator-curated global knowledge (vector-only), PROJECT-WINS. Opt-in; fail-soft —
    # a curated read error never breaks the project search. The curated tier is a SEPARATE collection.
    if r.include_curated:
        try:
            cur = mem_curated.search(r.query, agent_id=curate.CURATED_AGENT_ID, limit=r.limit)
            proj = curate.merge_project_wins(proj, cur, r.limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("curated fan-in failed (project results returned unchanged): %s", exc)
    return proj


class PromoteReq(BaseModel):
    from_agent_id: str             # the source partition (mem_ns) to promote FROM
    memory: str | None = None      # an operator-redacted memory text to promote
    query: str | None = None       # OR a query whose source-partition top matches are copied (by content)
    limit: int = 5
    confirm: bool = False          # operator gate: must be explicitly true (fail-closed, AD-9)


@app.post("/promote")
def promote(r: PromoteReq):
    """Operator-gated promotion into the curated-global tier (#634 / ADR-0011 AD-9): copy redacted,
    cross-project knowledge FROM a source partition INTO the SEPARATE curated_global collection — NEVER the
    normal /add path. Fail-closed (curate.promote_refusal): requires confirm=true + a source scope + EXACTLY
    ONE of `memory` (operator-redacted text) / `query` (source matches copied by content, no re-extraction).
    Writes ONLY into curated_global (agent_id=curated_global), never agent_memory."""
    if (err := curate.promote_refusal(confirm=r.confirm, from_agent_id=r.from_agent_id,
                                      memory=r.memory, query=r.query)):
        raise HTTPException(status_code=400, detail=err)
    if r.memory:
        res = mem_curated.add([{"role": "user", "content": r.memory}],
                              agent_id=curate.CURATED_AGENT_ID, infer=False)
        return {"promoted": 1, "result": res}
    hits = (mem_vec.search(r.query, agent_id=r.from_agent_id, limit=r.limit) or {}).get("results", [])
    copied = []
    for h in hits:
        txt = h.get("memory") or h.get("text")
        if txt:
            copied.append(mem_curated.add([{"role": "user", "content": txt}],
                                          agent_id=curate.CURATED_AGENT_ID, infer=False))
    return {"promoted": len(copied), "results": copied}


@app.get("/memories")
def get_all(user_id: str | None = None, agent_id: str | None = None, run_id: str | None = None):
    return mem.get_all(user_id=user_id, agent_id=agent_id, run_id=run_id)


@app.post("/delete_all")
def delete_all(r: DeleteReq):
    """Forget a whole partition (ADR-0011 D5 / Ironclad #601 S14-5): delete every memory matching the given
    user_id/agent_id/run_id filter, from BOTH the graph+vector store (``mem``) and the vector-only store
    (``mem_vec``). The engine's scope-aware forget targets ``agent_id`` (the project/track partition).

    **Fail-closed (AD-4):** `agent_id` (the `mem_ns` partition) is REQUIRED — a `run_id`/`user_id`-only delete
    would cut across partitions, and an all-empty request would wipe the entire store, so both are refused with
    HTTP 400. `run_id` is rejected as an isolation key (same rule as writes/searches); `user_id` may still
    narrow within the partition."""
    if (err := scope_guard.require_scope(r.agent_id, r.run_id)):
        raise HTTPException(status_code=400, detail=err)
    res = mem.delete_all(user_id=r.user_id, agent_id=r.agent_id, run_id=r.run_id)
    try:
        mem_vec.delete_all(user_id=r.user_id, agent_id=r.agent_id, run_id=r.run_id)
    except Exception:  # noqa: BLE001 — vector store may already be clear; the graph delete above is authoritative
        pass
    return res or {"status": "ok"}


@app.get("/scopes")
def scopes():
    """Distinct memory partitions (``agent_id`` = ``mem_ns``) present in the store (ADR-0011 AD-4 /
    Ironclad #601 S15) — the input to the engine's registry-keyed **orphan GC** (a minted partition with no
    registered project is an orphan). Scrolls the Qdrant collection's payloads and returns the distinct
    ``agent_id`` set. **Fail-soft**: any backend error yields ``{"scopes": []}`` so a read-only listing can
    never break (and the GC, which only ever *adds* deletes for orphans, simply finds nothing)."""
    found: set = set()
    try:
        from qdrant_client import QdrantClient
        qc = QdrantClient(
            host=os.environ.get("QDRANT_HOST", "qdrant"),
            port=int(os.environ.get("QDRANT_PORT", "6333")),
        )
        next_page = None
        while True:
            points, next_page = qc.scroll(
                collection_name=CONFIG["vector_store"]["config"]["collection_name"],
                with_payload=["agent_id"], with_vectors=False, limit=256, offset=next_page,
            )
            for p in points:
                aid = (getattr(p, "payload", None) or {}).get("agent_id")
                if isinstance(aid, str) and aid:
                    found.add(aid)
            if next_page is None:
                break
    except Exception as exc:  # noqa: BLE001 — read-only listing must never raise
        # Return an EMPTY list on any error (not a partial page): under-reporting present scopes only ever
        # makes the orphan GC flag FEWER orphans (safe — it never over-deletes); partial results could mislead.
        log.warning("/scopes scroll failed: %s", exc)
        return {"scopes": []}
    return {"scopes": sorted(found)}
