"""Reasoning workers — server-side fan-out to concurrent local-model requests.

> **The Spark half of the parallelism story.** The orchestrator's main conversation
> is serial by design (one agent, one lock). This module adds the *other* axis:
> independent **reasoning / planning** sub-tasks fired CONCURRENTLY at the local model
> (vLLM, co-located with the GPU). Stateless, no shared conversation, **no code
> access** — pure model calls. Because vLLM batches concurrent sequences
> (``max_num_seqs``), N independent prompts complete in roughly one prompt's
> wall-clock, not N times it.

Use it for anything that decomposes into independent model calls: parallel analysis
of many items, multi-candidate planning (a judge panel), batch classification. It
does NOT touch the agent state, so it runs alongside a /chat turn without contending
for the agent lock.

The transport is the same OpenAI-compatible client the engine already holds; only the
concurrency and the per-call isolation live here. Each worker returns a result dict
(never raises) so one failed prompt can't sink the batch, and results are returned in
the SAME ORDER as the input prompts.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence

#: vLLM's default ``max_num_seqs`` on the Spark deploy — the natural concurrency cap
#: (more in-flight requests than batch slots just queue server-side).
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_MAX_TOKENS = 2048


class ReasoningWorkers:
    """Bounded fan-out of stateless reasoning calls against one chat model.

    ``client`` is an OpenAI-compatible client (``client.chat.completions.create``).
    ``max_concurrency`` caps in-flight requests (default = vLLM batch width). The
    client is shared read-only across worker threads — each ``create`` call is
    independent, which the underlying httpx transport handles concurrently.
    """

    def __init__(self, client: Any, model: str, *,
                 max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
                 default_max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.client = client
        self.model = model
        self.max_concurrency = max(1, int(max_concurrency))
        self.default_max_tokens = int(default_max_tokens)

    def _one(self, prompt: str, system: Optional[str], max_tokens: int,
             temperature: float, think: bool) -> Dict[str, Any]:
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        t0 = time.monotonic()
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body={"chat_template_kwargs": {"enable_thinking": think}},
            )
            choice = resp.choices[0]
            content = getattr(choice.message, "content", None) or ""
            usage = getattr(resp, "usage", None)
            return {
                "ok": True,
                "content": content,
                "error": None,
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "latency": round(time.monotonic() - t0, 3),
            }
        except Exception as e:  # noqa: BLE001 — isolate: one bad prompt ≠ batch failure
            return {
                "ok": False,
                "content": None,
                "error": repr(e),
                "completion_tokens": None,
                "latency": round(time.monotonic() - t0, 3),
            }

    def fanout(self, prompts: Sequence[str], *, system: Optional[str] = None,
               max_tokens: Optional[int] = None, temperature: float = 0.7,
               think: bool = True) -> List[Dict[str, Any]]:
        """Run every prompt concurrently (bounded) and return results IN INPUT ORDER.

        ``think`` defaults to True — these are reasoning/planning calls, so the
        model's thinking stays on (unlike the ACK structured-emission path, which
        forces it off). Never raises for a model/transport error on a single prompt;
        that prompt's result carries ``ok=False`` + ``error``.
        """
        n = len(prompts)
        if n == 0:
            return []
        mt = max_tokens or self.default_max_tokens
        results: List[Optional[Dict[str, Any]]] = [None] * n
        workers = min(self.max_concurrency, n)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reason") as pool:
            futs = {
                pool.submit(self._one, p, system, mt, temperature, think): i
                for i, p in enumerate(prompts)
            }
            for f in as_completed(futs):
                results[futs[f]] = f.result()
        return [r for r in results if r is not None]
