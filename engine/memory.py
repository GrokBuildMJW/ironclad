"""Optional long-term memory backend (Mem0-compatible HTTP service).

The orchestrator has memory *hooks* (a ``query_memory`` tool, store-on-task-completion,
stage-time context injection); this module is the backend they call. It is **optional**
and **off unless configured** — when no endpoint is set, ``MemoryManager`` is never
constructed and the hooks stay inert.

Secret-free: the endpoint comes from config / ``GX10_MEMORY_URL`` at runtime, never
hard-coded. Talks to a Mem0-style service over plain HTTP:

  * ``POST /add``      — ``{messages, agent_id?, user_id?, metadata?}`` (LLM-inferred
    extraction; slow → done fire-and-forget on task completion)
  * ``POST /add_bulk`` — ``{messages, agent_id?, user_id?, metadata?, infer:false}`` (raw
    vector-only store, no LLM → fast; used to archive context evicted from the live window
    losslessly, so nothing is lost on a trim — B1).
  * ``POST /search`` — ``{query, agent_id?, user_id?, limit, graph}`` → ``{"results":[…]}``
    The read path sends ``graph=false`` (the graph store can time out; vector search is
    the fast, reliable path).
  * ``GET /health``  — liveness.

The contract the engine relies on (see ``gx10.py`` call sites):
``is_available() -> bool``, ``store_task_completion(task_id, task, feedback) -> None``,
``add_bulk(text, metadata) -> None``, ``chunk_and_store(text, metadata) -> None``,
``get_context(task_type, title) -> str``, ``search(query, limit) -> List[str]``,
``query(query, limit) -> str``, ``deep_query(query, limit) -> str`` (opt-in graph path).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class MemoryManager:
    """Thin client for a Mem0-style memory service. All methods are fail-soft: a
    transport/timeout error degrades to "no memory", never raises into the engine."""

    #: feedback length kept inline in the inferred /add episode; beyond this the full text is
    #: lost unless B3 chunking is on (then ``chunk_and_store`` archives it passage-level).
    _FEEDBACK_CAP = 4000

    def __init__(self, config: Dict[str, Any]) -> None:
        self.base = str(config.get("base_url") or "").rstrip("/")
        self.enabled = bool(config.get("enabled", bool(self.base)))
        self.agent_id = config.get("agent_id") or "ironclad"
        self.user_id = config.get("user_id") or None
        # /add runs LLM extraction + graph build server-side → slow (tens of seconds).
        # It's fire-and-forget on a daemon thread, so a generous timeout is safe.
        self.add_timeout = float(config.get("add_timeout", config.get("timeout", 120.0)))
        self.read_timeout = float(config.get("read_timeout", 15.0))
        # graph (multi-hop) search runs LLM entity extraction server-side → slow; only the opt-in
        # deep_query path uses it, with this generous timeout (the hot read stays vector-only).
        self.deep_timeout = float(config.get("deep_timeout", 40.0))
        self._health_ttl = float(config.get("health_ttl", 10.0))
        self._health_at = 0.0
        self._health_ok = False
        # B3 — long-artifact chunking + recency ranking. Default ON (06-18 decision); set
        # chunk_long_artifacts / recency_tiebreak = false to restore truncate-and-store /
        # server-order behaviour.
        self.chunk_long = bool(config.get("chunk_long_artifacts", True))
        self.chunk_size = int(config.get("chunk_size", 6000))      # chars (~1.5k tokens)
        self.chunk_overlap = int(config.get("chunk_overlap", 400))  # passage overlap (chars)
        self.recency_tiebreak = bool(config.get("recency_tiebreak", True))

    # ── transport ────────────────────────────────────────────
    def _ids(self) -> Dict[str, Any]:
        ids: Dict[str, Any] = {"agent_id": self.agent_id}
        if self.user_id:
            ids["user_id"] = self.user_id
        return ids

    def _post(self, path: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    # ── liveness (short TTL cache so the tool-list / gates don't hammer /health) ──
    def is_available(self) -> bool:
        if not self.enabled or not self.base:
            return False
        now = time.monotonic()
        if now - self._health_at < self._health_ttl:
            return self._health_ok
        ok = False
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=4) as r:
                d = json.loads(r.read().decode("utf-8") or "{}")
                ok = (d.get("status") == "ok") or (200 <= r.status < 300)
        except Exception:  # noqa: BLE001 — down = unavailable
            ok = False
        self._health_at, self._health_ok = now, ok
        return ok

    # ── write (fire-and-forget: /add runs LLM inference, can take seconds) ──
    def store_task_completion(self, task_id: str, task: Dict[str, Any], feedback: str) -> None:
        if not self.enabled or not self.base:
            return
        content = self._episode(task_id, task or {}, feedback or "")

        def _go() -> None:
            try:
                self._post("/add", {
                    "messages": [{"role": "user", "content": content}],
                    "metadata": {"task_id": task_id, "type": (task or {}).get("type"),
                                 "title": (task or {}).get("title"), "source": "task_completion"},
                    **self._ids(),
                }, self.add_timeout)
            except Exception:  # noqa: BLE001 — best effort, never break advance
                pass

        threading.Thread(target=_go, daemon=True, name="mem-add").start()

        # B3: losslessly chunk-store feedback that exceeds the episode cap (flag-gated), so the
        # FULL artifact stays retrievable passage-level instead of being truncated. Additive —
        # the inferred /add episode above is unchanged; flag OFF ⇒ byte-identical to today.
        if self.chunk_long and len(feedback or "") > self._FEEDBACK_CAP:
            self.chunk_and_store(
                feedback,
                {"task_id": task_id, "type": (task or {}).get("type"),
                 "title": (task or {}).get("title")},
                source="task_completion",
            )

    @staticmethod
    def _episode(task_id: str, task: Dict[str, Any], feedback: str) -> str:
        head = f"Task {task_id} completed."
        meta = []
        if task.get("type"):
            meta.append(f"type={task['type']}")
        if task.get("title"):
            meta.append(f"title: {task['title']}")
        if task.get("description"):
            meta.append(f"scope: {task['description']}")
        body = "  ".join(meta)
        cap = MemoryManager._FEEDBACK_CAP
        fb = f"\nResult / feedback:\n{feedback[:cap]}" if feedback.strip() else ""
        return f"{head} {body}{fb}".strip()

    # ── bulk write (raw, vector-only, no LLM → fast) ──
    def add_bulk(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Archive *text* to the cold store via ``/add_bulk`` (``infer=False`` → pure vector,
        no LLM extraction → fast). Fire-and-forget on a daemon thread; fail-soft. Used by B1
        to losslessly preserve rounds evicted from the live window, so a trim never loses data.
        Distinct from ``store_task_completion`` (which uses the slow inferred ``/add``)."""
        if not self.enabled or not self.base or not (text or "").strip():
            return
        md: Dict[str, Any] = {"source": "context_eviction"}
        if metadata:
            md.update(metadata)

        def _go() -> None:
            try:
                self._post("/add_bulk", {
                    "messages": [{"role": "user", "content": text}],
                    "metadata": md, "infer": False, **self._ids(),
                }, self.read_timeout)  # bulk has no LLM → the short read timeout is enough
            except Exception:  # noqa: BLE001 — best effort, never break the turn
                pass

        threading.Thread(target=_go, daemon=True, name="mem-bulk").start()

    # ── chunked write (lossless long-artifact store, B3) ──
    @staticmethod
    def _chunks(text: str, size: int, overlap: int) -> List[str]:
        """Split *text* into overlapping passages of ≤ *size* chars (step = size − overlap).
        Overlap keeps a phrase that straddles a boundary retrievable. Covers the whole text."""
        text = text or ""
        if not text.strip():
            return []
        if size <= 0:
            return [text]
        step = max(1, size - max(0, overlap))
        out: List[str] = []
        i, n = 0, len(text)
        while i < n:
            out.append(text[i:i + size])
            if i + size >= n:
                break
            i += step
        return out

    def chunk_and_store(self, text: str, metadata: Optional[Dict[str, Any]] = None,
                        *, source: str = "artifact") -> None:
        """Chunk a long artifact into overlapping passages and store each via ``/add_bulk``
        (vector-only, ``infer=False`` → fast). Makes a big document retrievable passage-level
        WITHOUT it ever entering the model window whole — no 4000-char truncation loss (B3).
        Fire-and-forget on one daemon thread; fail-soft; no-op when disabled / empty."""
        if not self.enabled or not self.base:
            return
        chunks = self._chunks(text or "", self.chunk_size, self.chunk_overlap)
        if not chunks:
            return
        base_md: Dict[str, Any] = {"source": source}
        if metadata:
            base_md.update(metadata)
        n = len(chunks)

        def _go() -> None:
            for i, ch in enumerate(chunks):
                try:
                    self._post("/add_bulk", {
                        "messages": [{"role": "user", "content": ch}],
                        "metadata": {**base_md, "chunk": i, "chunks": n},
                        "infer": False, **self._ids(),
                    }, self.read_timeout)
                except Exception:  # noqa: BLE001 — best effort per chunk, never break a turn
                    pass

        threading.Thread(target=_go, daemon=True, name="mem-chunk").start()

    # ── read (vector-only by default: graph=false; deep_query opts into graph=true) ──
    def _search(self, query: str, limit: int, graph: bool = False,
                timeout: Optional[float] = None) -> List[str]:
        if not query.strip() or not self.base:
            return []
        try:
            d = self._post("/search", {"query": query, "limit": int(limit),
                                       "graph": bool(graph), **self._ids()}, timeout or self.read_timeout)
        except Exception:  # noqa: BLE001
            return []
        results = d.get("results") or []
        # B3: optionally re-rank by relevance then recency (flag-gated). OFF ⇒ server order,
        # byte-identical to today.
        if self.recency_tiebreak:
            results = self._rank_recency(results)
        out: List[str] = []
        for it in results:
            if isinstance(it, dict):
                t = it.get("memory") or it.get("text") or it.get("content")
                if t:
                    out.append(str(t))
            elif isinstance(it, str):
                out.append(it)
        return out

    @staticmethod
    def _rank_recency(results: List[Any]) -> List[Any]:
        """Stable re-rank: relevance ``score`` desc, ties broken by ``created_at`` desc (most
        recent first). Missing fields sort neutrally; stable ⇒ equal keys keep server order."""
        def key(it: Any):
            if not isinstance(it, dict):
                return (0.0, "")
            try:
                s = round(float(it.get("score")), 3)
            except (TypeError, ValueError):
                s = 0.0
            created = it.get("created_at") or (it.get("metadata") or {}).get("created_at") or ""
            return (s, str(created))
        return sorted(results, key=key, reverse=True)

    def search(self, query: str, limit: int = 5) -> List[str]:
        """Raw vector-only retrieval (a list of memory strings) for per-turn RAG assembly (B2).
        Public, fail-soft wrapper over the vector path (``graph=false``) → ``[]`` on any error."""
        return self._search(query, limit)

    def get_context(self, task_type: str, title: str) -> str:
        """Past-pattern context appended to a handover at stage time (or "")."""
        q = f"{task_type}: {title}".strip(" :")
        hits = self._search(q, 5)
        if not hits:
            return ""
        return "## Relevant context from memory\n" + "\n".join(f"- {h}" for h in hits)

    def query(self, query: str, limit: int = 8) -> str:
        """Formatted result for the ``query_memory`` tool (vector-only hot path)."""
        hits = self._search(query, limit)
        if not hits:
            return "[Memory] no relevant matches."
        return "[Memory] matches:\n" + "\n".join(f"- {h}" for h in hits)

    def deep_query(self, query: str, limit: int = 5) -> str:
        """§3-Mechanismus 5 / MEM-10: RELATIONAL / multi-hop retrieval via the GRAPH path
        (``graph=true``, generous ``deep_timeout``) for the ``deep_query_memory`` tool. Slower and
        kept OFF the hot read path (which stays vector-only). Fail-soft → a formatted string. Use
        for connection/dependency questions, not routine lookups."""
        hits = self._search(query, limit, graph=True, timeout=self.deep_timeout)
        if not hits:
            return "[Memory] no relational matches."
        return "[Memory] graph matches:\n" + "\n".join(f"- {h}" for h in hits)
