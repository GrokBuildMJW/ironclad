"""Native Brave Search HTTP adapter (epic #505, S4) — the working native web-search path.

This module is the single home for every Brave-specific literal (the API host and the
subscription header). The rest of the engine selects it only by the vendor-neutral
``search.adapter`` value and talks to it through the :class:`~websearch_adapters.WebSearchAdapter`
seam, so swapping or adding a backend never touches the gate, prompt, renderer or tool schema.

stdlib-only (epic #505 Fork 1: ``urllib.request``, no httpx/requests — the standalone wheel stays
pydantic-only). The adapter is stateless, read-only, timeout-bounded and fail-soft: a failed or
timed-out search returns a short readable note, never an exception into the tool loop. Native
search is local-only (Fork 2); the builder wires this adapter only on a local setup with a key.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional, Tuple

from websearch_adapters import SearchBatch, SearchHit, SearchOutput, WebSearchAdapter

# The only Brave-specific literals in the engine (epic #505 S4 / R1 — vendor confinement).
_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TOKEN_HEADER = "X-Subscription-Token"


def _build_query(query: str, allow_domains: Tuple[str, ...],
                 block_domains: Tuple[str, ...]) -> str:
    """Apply the domain filters BEFORE the search via the provider's ``site:`` operators (the
    validator already guaranteed allow XOR block, normalized, wildcard-free)."""
    if allow_domains:
        sites = " OR ".join(f"site:{d}" for d in allow_domains)
        return f"{query} ({sites})" if len(allow_domains) > 1 else f"{query} site:{allow_domains[0]}"
    if block_domains:
        return query + "".join(f" -site:{d}" for d in block_domains)
    return query


def _normalize(payload: Any) -> Tuple[SearchHit, ...]:
    """Provider web results → SearchHits. Defensive: the response shape can be partial or absent."""
    web = payload.get("web") if isinstance(payload, dict) else None
    results = web.get("results") if isinstance(web, dict) else None
    if not isinstance(results, list):
        return ()
    hits = []
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        hits.append(SearchHit(title=str(r.get("title") or url), url=str(url),
                              snippet=str(r.get("description") or "")))
    return tuple(hits)


class BraveAdapter(WebSearchAdapter):
    """Calls the Brave Search API directly over stdlib HTTP and normalizes the response."""

    name = "native"   # the engine never sees a vendor name; the literal stays inside this module

    def __init__(self, api_key: str, *, endpoint: str = _ENDPOINT, count: int = 10,
                 timeout_s: float = 10.0, opener: Optional[Callable[..., Any]] = None) -> None:
        self._key = api_key or ""
        self._endpoint = endpoint
        self._count = max(1, min(int(count), 20))
        self._timeout = float(timeout_s)
        # Injectable opener (default = the stdlib urlopen) keeps the adapter network-free under test.
        self._open = opener or urllib.request.urlopen

    def available(self) -> bool:
        return bool(self._key)

    def run(self, query: str, allow_domains: Tuple[str, ...] = (),
            block_domains: Tuple[str, ...] = ()) -> SearchOutput:
        t0 = time.monotonic()
        if not self._key:
            return SearchOutput(query=query,
                                results=("[web_search] unavailable — the search API key is not set.",))
        q = _build_query(query, allow_domains, block_domains)
        url = self._endpoint + "?" + urllib.parse.urlencode({"q": q, "count": self._count})
        req = urllib.request.Request(url, headers={_TOKEN_HEADER: self._key,
                                                   "Accept": "application/json"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            ms = int((time.monotonic() - t0) * 1000)
            return SearchOutput(query=query, results=(f"[web_search] the search failed (HTTP {e.code}).",),
                                duration_ms=ms)
        except Exception:  # noqa: BLE001 — timeout / network / decode → a readable note, never a raise
            ms = int((time.monotonic() - t0) * 1000)
            return SearchOutput(query=query, results=("[web_search] the search could not be completed.",),
                                duration_ms=ms)
        ms = int((time.monotonic() - t0) * 1000)
        return SearchOutput(query=query, results=(SearchBatch("hit-batch-1", _normalize(payload)),),
                            duration_ms=ms)
