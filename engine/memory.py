"""Optional long-term memory backend (Mem0-compatible HTTP service).

The orchestrator has memory *hooks* (a ``query_memory`` tool, store-on-task-completion,
stage-time context injection); this module is the backend they call. It is **optional**
and **off unless configured** — when no endpoint is set, ``MemoryManager`` is never
constructed and the hooks stay inert.

Secret-free: the endpoint comes from config / ``GX10_MEMORY_URL`` at runtime, never
hard-coded. Talks to a Mem0-style service over plain HTTP:

  * ``POST /add``    — ``{messages, agent_id?, user_id?, metadata?}`` (LLM-inferred
    extraction; slow → done fire-and-forget on task completion)
  * ``POST /search`` — ``{query, agent_id?, user_id?, limit, graph}`` → ``{"results":[…]}``
    The read path sends ``graph=false`` (the graph store can time out; vector search is
    the fast, reliable path).
  * ``GET /health``  — liveness.

The contract the engine relies on (see ``gx10.py`` call sites):
``is_available() -> bool``, ``store_task_completion(task_id, task, feedback) -> None``,
``get_context(task_type, title) -> str``, ``query(query, limit) -> str``.
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

    def __init__(self, config: Dict[str, Any]) -> None:
        self.base = str(config.get("base_url") or "").rstrip("/")
        self.enabled = bool(config.get("enabled", bool(self.base)))
        self.agent_id = config.get("agent_id") or "ironclad"
        self.user_id = config.get("user_id") or None
        # /add runs LLM extraction + graph build server-side → slow (tens of seconds).
        # It's fire-and-forget on a daemon thread, so a generous timeout is safe.
        self.add_timeout = float(config.get("add_timeout", config.get("timeout", 120.0)))
        self.read_timeout = float(config.get("read_timeout", 15.0))
        self._health_ttl = float(config.get("health_ttl", 10.0))
        self._health_at = 0.0
        self._health_ok = False

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
        fb = f"\nResult / feedback:\n{feedback[:4000]}" if feedback.strip() else ""
        return f"{head} {body}{fb}".strip()

    # ── read (vector-only: graph=false) ──
    def _search(self, query: str, limit: int) -> List[str]:
        if not query.strip() or not self.base:
            return []
        try:
            d = self._post("/search", {"query": query, "limit": int(limit),
                                       "graph": False, **self._ids()}, self.read_timeout)
        except Exception:  # noqa: BLE001
            return []
        out: List[str] = []
        for it in (d.get("results") or []):
            if isinstance(it, dict):
                t = it.get("memory") or it.get("text") or it.get("content")
                if t:
                    out.append(str(t))
            elif isinstance(it, str):
                out.append(it)
        return out

    def get_context(self, task_type: str, title: str) -> str:
        """Past-pattern context appended to a handover at stage time (or "")."""
        q = f"{task_type}: {title}".strip(" :")
        hits = self._search(q, 5)
        if not hits:
            return ""
        return "## Relevant context from memory\n" + "\n".join(f"- {h}" for h in hits)

    def query(self, query: str, limit: int = 8) -> str:
        """Formatted result for the ``query_memory`` tool."""
        hits = self._search(query, limit)
        if not hits:
            return "[Memory] no relevant matches."
        return "[Memory] matches:\n" + "\n".join(f"- {h}" for h in hits)
