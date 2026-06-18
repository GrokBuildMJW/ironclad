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

#: Conservative, model-agnostic defaults — SAFE for an unknown endpoint, NOT tuned for
#: throughput. The deploy pins the model-matched values in private config (for our
#: reference model: concurrency 8 = qwen3.6-35b's ``max_num_seqs``). Sending more
#: requests than the batch width never crashes vLLM — they queue server-side — but on a
#: bandwidth-limited box too many *long* concurrent generations thrash the KV cache, so
#: we also bound the in-flight token budget (the envelope below).
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MAX_TOKENS = 1024
#: Safety envelope: the worst-case in-flight generation (``concurrency × max_tokens``)
#: is kept at or below this. A larger per-call ``max_tokens`` automatically lowers the
#: effective concurrency so the GPU is never over-subscribed — overflow just queues.
DEFAULT_MAX_BATCH_TOKENS = 8192


class ReasoningWorkers:
    """Bounded fan-out of stateless reasoning calls against one chat model.

    ``client`` is an OpenAI-compatible client (``client.chat.completions.create``).
    ``max_concurrency`` caps in-flight requests (≈ the vLLM batch width); ``max_batch_
    tokens`` is the safety envelope that keeps ``concurrency × max_tokens`` bounded so a
    fan-out can never over-subscribe the GPU regardless of the requested token count.
    The client is shared read-only across worker threads — each ``create`` call is
    independent, which the underlying httpx transport handles concurrently.
    """

    def __init__(self, client: Any, model: str, *,
                 max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
                 default_max_tokens: int = DEFAULT_MAX_TOKENS,
                 max_batch_tokens: int = DEFAULT_MAX_BATCH_TOKENS) -> None:
        self.client = client
        self.model = model
        self.max_concurrency = max(1, int(max_concurrency))
        self.default_max_tokens = int(default_max_tokens)
        self.max_batch_tokens = max(1, int(max_batch_tokens))

    def _plan_concurrency(self, n: int, max_tokens: int) -> int:
        """Effective parallelism for a batch of ``n`` prompts each allowed ``max_tokens``:
        the min of the request size, the configured concurrency, and the token-budget
        envelope (``max_batch_tokens // max_tokens``). Always ≥ 1 — a single oversized
        call still runs (alone), it just never fans out beyond the safe budget."""
        budget_cap = max(1, self.max_batch_tokens // max(1, int(max_tokens)))
        return max(1, min(self.max_concurrency, n, budget_cap))

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
        # Safety governor: cap parallelism by the token-budget envelope, not just the
        # request size — a large max_tokens lowers concurrency so the box is never
        # over-subscribed; the rest of the batch simply queues behind the pool.
        workers = self._plan_concurrency(n, mt)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reason") as pool:
            futs = {
                pool.submit(self._one, p, system, mt, temperature, think): i
                for i, p in enumerate(prompts)
            }
            for f in as_completed(futs):
                results[futs[f]] = f.result()
        return [r for r in results if r is not None]
