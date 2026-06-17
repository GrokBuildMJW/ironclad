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

from fastapi import FastAPI
from pydantic import BaseModel
from mem0 import Memory

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

app = FastAPI(title="Agent Memory (Mem0)", version="1.0")

# === Reflection (central, threshold-triggered) =============================
# Counts learning writes (infer=True); once REFLECT_EVERY new ones accumulate it fires
# in the background (non-blocking, lock = only one at a time). Safe: graph hygiene
# (merge duplicate entities of the same name+scope). Destructive pruning: off / TODO.
REFLECT_EVERY = int(os.environ.get("REFLECT_EVERY_N_WRITES", "50"))
REFLECT_STATE = os.environ.get("REFLECT_STATE_PATH", "/hf-cache/reflect_state.json")
_reflect_lock = threading.Lock()
_state_lock = threading.Lock()


def _load_state() -> dict:
    try:
        with open(REFLECT_STATE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"writes_since": 0, "last_run": None, "last_summary": None}


def _save_state(s: dict) -> None:
    try:
        with open(REFLECT_STATE, "w") as f:
            json.dump(s, f)
    except Exception as exc:  # noqa: BLE001
        log.warning("reflect state save: %s", exc)


def _graph_hygiene() -> dict:
    """Safe + non-destructive for data: merge entities with the same name+scope
    (relations preserved via mergeRels)."""
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(
        os.environ.get("NEO4J_URL", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    try:
        with drv.session() as s:
            nb = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            rb = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            merged = s.run(
                "MATCH (n) WHERE n.name IS NOT NULL "
                "WITH n.name AS nm, n.user_id AS uid, n.agent_id AS aid, collect(n) AS ns "
                "WHERE size(ns) > 1 "
                "CALL apoc.refactor.mergeNodes(ns, {properties:'discard', mergeRels:true}) YIELD node "
                "RETURN count(*) AS m"
            ).single()["m"]
            na = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            ra = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        return {"nodes_before": nb, "nodes_after": na, "rels_before": rb, "rels_after": ra,
                "duplicate_groups_merged": merged}
    finally:
        drv.close()


def _run_reflection(reason: str) -> None:
    if not _reflect_lock.acquire(blocking=False):
        log.info("reflection already running — skip (%s)", reason)
        return
    try:
        t0 = time.time()
        log.info("reflection START (%s)", reason)
        summary = {"reason": reason, "error": None}
        try:
            summary["graph"] = _graph_hygiene()
        except Exception as exc:  # noqa: BLE001
            summary["error"] = str(exc)
            log.warning("reflection error: %s", exc)
        summary["seconds"] = round(time.time() - t0, 1)
        with _state_lock:
            st = _load_state()
            st["writes_since"] = 0
            st["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            st["last_summary"] = summary
            _save_state(st)
        log.info("reflection DONE: %s", summary)
    finally:
        _reflect_lock.release()


def _maybe_reflect() -> None:
    with _state_lock:
        st = _load_state()
        st["writes_since"] = st.get("writes_since", 0) + 1
        fire = st["writes_since"] >= REFLECT_EVERY
        _save_state(st)
    if fire:
        threading.Thread(target=_run_reflection, args=(f"threshold/{REFLECT_EVERY}",), daemon=True).start()


@app.post("/reflect")
def reflect_now():
    """Manual trigger (background)."""
    threading.Thread(target=_run_reflection, args=("manual",), daemon=True).start()
    return {"status": "reflection started (background)"}


@app.get("/reflect/status")
def reflect_status():
    st = _load_state()
    st["reflect_every_n_writes"] = REFLECT_EVERY
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/add")
def add(r: AddReq):
    res = mem.add(
        r.messages, user_id=r.user_id, agent_id=r.agent_id,
        run_id=r.run_id, metadata=r.metadata, infer=r.infer,
    )
    if r.infer:
        _maybe_reflect()
    return res


@app.post("/add_bulk")
def add_bulk(r: AddReq):
    """Bulk: vector only (no graph, no LLM) → fast. infer always False."""
    return mem_vec.add(
        r.messages, user_id=r.user_id, agent_id=r.agent_id,
        run_id=r.run_id, metadata=r.metadata, infer=False,
    )


@app.post("/search")
def search(r: SearchReq):
    # graph=False → mem_vec (pure vector search). The graph search of `mem` extracts
    # query entities via the LLM and occasionally tripped the client read timeout. For
    # retrieval/dedup the vector path is enough.
    inst = mem if r.graph else mem_vec
    return inst.search(
        r.query, user_id=r.user_id, agent_id=r.agent_id,
        run_id=r.run_id, limit=r.limit,
    )


@app.get("/memories")
def get_all(user_id: str | None = None, agent_id: str | None = None, run_id: str | None = None):
    return mem.get_all(user_id=user_id, agent_id=agent_id, run_id=run_id)
