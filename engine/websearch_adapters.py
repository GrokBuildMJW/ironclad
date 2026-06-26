"""Web-search adapter seam (epic #505, S3) — a vendor-neutral, standalone selection layer.

Built at boot (server.py) from the ``search`` config block and held as the ``gx10._WEBSEARCH``
global, INDEPENDENT of the provider-dispatcher registry (epic #505 Fork 3): the brave and mock
adapters never touch the registry, so a native-search deployment with no CLI provider still
offers ``web_search`` (the registry-bolted gate would have dead-gated it). Only the CLI-delegate
adapter rides the existing dispatcher lane.

Every adapter returns a structured :class:`SearchOutput` so the downstream stages have a stable
shape to consume: S5 (the deterministic ``Sources:`` block + max-output cap) and S9 (the ``web N
· Xms`` footer needs the batch count + duration). stdlib-only — no new runtime dependency
(Fork 1: the native Brave adapter, S4, will use ``urllib.request``, not httpx).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Union


# ── structured output contract (epic #505 S3 / clean-room spec) ──────────────
@dataclass(frozen=True)
class SearchHit:
    """One search result with a real link."""
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class SearchBatch:
    """A group of hits from one provider request (the structured half of the output)."""
    request_id: str
    hits: Tuple[SearchHit, ...] = ()


# A result item is either a structured batch (real links) or a plain string (a note / summary /
# the CLI-delegate's opaque text). Mirrors the spec's ``Array<SearchBatch | string>``.
ResultItem = Union[SearchBatch, str]


@dataclass(frozen=True)
class SearchOutput:
    """The seam's return contract: the query, the mixed result list, and the measured runtime."""
    query: str
    results: Tuple[ResultItem, ...] = ()
    duration_ms: int = 0

    def batch_count(self) -> int:
        """Number of real search batches (spec test 12: the UI summary counts only these,
        skipping string notes)."""
        return sum(1 for r in self.results if isinstance(r, SearchBatch))

    def all_hits(self) -> List[SearchHit]:
        hits: List[SearchHit] = []
        for r in self.results:
            if isinstance(r, SearchBatch):
                hits.extend(r.hits)
        return hits

    def to_model_text(self) -> str:
        """A clean, model-facing rendering of the results (the spec's
        ``formatSearchResultForModel``, minus the mandatory sources reminder — S5 adds that and
        the max-output cap on top)."""
        lines: List[str] = [f'Web search results for: "{self.query}"', ""]
        for item in self.results:
            if isinstance(item, str):
                if item.strip():
                    lines.append(item.strip())
                    lines.append("")
                continue
            if not item.hits:
                lines.append("No links found.")
                lines.append("")
                continue
            lines.append("Links:")
            for h in item.hits:
                lines.append(f"- {h.title}: {h.url}")
            lines.append("")
        return "\n".join(lines).strip()


# ── model-facing formatter (epic #505 S5) ───────────────────────────────────
#: The spec's maxOutputCharacters guard against an oversized tool result.
DEFAULT_MAX_OUTPUT_CHARS = 100_000
_SOURCES_REMINDER = ("Reminder: include the relevant sources in your final answer as "
                     "Markdown links.")


def format_for_model(out: "SearchOutput", *,
                     max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS) -> str:
    """Render a :class:`SearchOutput` into the model-facing tool result (epic #505 S5 / D5):
    the clean result text, a deterministic ``Sources:`` list of the unique result URLs, the whole
    thing capped to *max_output_chars*, and an always-present sources reminder. The reminder is
    appended AFTER the cap so 'every web_search result ends with a sources reminder' is a testable
    invariant rather than a skippable prompt hint. The structured object itself stays internal to
    the renderer/sentinel path — the model only ever receives this clean text."""
    try:
        cap = int(max_output_chars)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_OUTPUT_CHARS
    if cap <= 0:
        cap = DEFAULT_MAX_OUTPUT_CHARS

    parts = [out.to_model_text()]
    seen: List[str] = []
    for h in out.all_hits():
        if h.url and h.url not in seen:
            seen.append(h.url)
    if seen:
        parts.append("")
        parts.append("Sources:")
        parts.extend(f"- {u}" for u in seen)

    text = "\n".join(parts)
    if len(text) > cap:
        text = text[:cap].rstrip()
    return f"{text}\n\n{_SOURCES_REMINDER}"


# ── adapters ─────────────────────────────────────────────────────────────────
class WebSearchAdapter:
    """Seam interface. ``available()`` is the central capability check (replaces the
    dispatcher-only ``has_web_provider`` gate); ``run`` executes one read-only, concurrency-safe
    search and never raises."""

    name = "base"

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def run(self, query: str, allow_domains: Tuple[str, ...] = (),
            block_domains: Tuple[str, ...] = ()) -> SearchOutput:  # pragma: no cover - overridden
        raise NotImplementedError


class CliDelegateAdapter(WebSearchAdapter):
    """Delegates to an external web-capable CLI provider via the existing dispatcher lane
    (route_one + the captured CLI runner). The one adapter that depends on the registry."""

    name = "cli"

    def __init__(self, dispatcher: Any) -> None:
        self._d = dispatcher

    def available(self) -> bool:
        return self._d is not None and bool(self._d.has_web_provider())

    def run(self, query: str, allow_domains: Tuple[str, ...] = (),
            block_domains: Tuple[str, ...] = ()) -> SearchOutput:
        t0 = time.monotonic()
        # S3->S4 hand-off: dispatch.web_search gains domain parameters in S4 (#509); the CLI
        # backend may not honour domain filters, so for now they are accepted but not forwarded.
        if self._d is None:
            res: dict = {"ok": False, "content": None, "error": "no-dispatcher"}
        else:
            res = self._d.web_search(query)
        ms = int((time.monotonic() - t0) * 1000)
        if res.get("ok") and res.get("content"):
            # Opaque CLI text → a single string result entry (epic #505 Fork R5: CLI-delegate emits
            # the text + the sources reminder line only; structured SearchBatch comes from S4/brave).
            return SearchOutput(query=query, results=(str(res["content"]).strip(),), duration_ms=ms)
        return SearchOutput(
            query=query,
            results=(f"[web_search] no result ({res.get('error') or 'empty'}).",),
            duration_ms=ms,
        )


class MockAdapter(WebSearchAdapter):
    """Deterministic, network-free adapter for tests and a zero-config demo. Always available."""

    name = "mock"

    def __init__(self, hits: Optional[Tuple[SearchHit, ...]] = None) -> None:
        self._hits = hits if hits is not None else (
            SearchHit("Example Domain", "https://example.com/", "Illustrative result."),
        )

    def available(self) -> bool:
        return True

    def run(self, query: str, allow_domains: Tuple[str, ...] = (),
            block_domains: Tuple[str, ...] = ()) -> SearchOutput:
        t0 = time.monotonic()
        ms = int((time.monotonic() - t0) * 1000)
        return SearchOutput(
            query=query,
            results=(SearchBatch(request_id="mock-1", hits=tuple(self._hits)),),
            duration_ms=ms,
        )


class UnavailableAdapter(WebSearchAdapter):
    """A configured-but-not-usable adapter: ``available()`` is False and ``run`` returns a clean
    reason (e.g. the native Brave adapter before S4, or ``search.enabled=false``)."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    def available(self) -> bool:
        return False

    def run(self, query: str, allow_domains: Tuple[str, ...] = (),
            block_domains: Tuple[str, ...] = ()) -> SearchOutput:
        return SearchOutput(query=query, results=(f"[web_search] unavailable — {self._reason}",))


def build_web_search_adapter(cfg: Optional[dict], dispatcher: Any,
                             *, runner_mode: str = "none") -> WebSearchAdapter:
    """Select the web-search adapter from ``cfg['search']`` (epic #505 Fork 3). Never raises.

    * ``mock`` → :class:`MockAdapter` (tests / demo).
    * ``brave`` → native search is **local-only** (Fork 2): on a local desktop setup the real
      :class:`~websearch_brave.BraveAdapter` lands in S4 (#509); under server mode (``runner_mode
      != 'local'``) it falls back to the CLI-delegate so server deployments still search.
    * anything else / ``cli`` / unset → :class:`CliDelegateAdapter` (today's behaviour; the
      backward-compatible default).
    """
    search = (cfg or {}).get("search") or {}
    if not search.get("enabled", True):
        return UnavailableAdapter("disabled", "web search is disabled (search.enabled=false)")
    adapter = str(search.get("adapter") or "cli").strip().lower()
    if adapter == "mock":
        return MockAdapter()
    if adapter == "brave":
        if runner_mode != "local":
            # Fork 2: server mode has no native egress for search → keep the CLI-delegate lane.
            return CliDelegateAdapter(dispatcher)
        # Local desktop: the native HTTP adapter, keyed name-indirectly from the environment.
        import os
        key = os.environ.get(str(search.get("api_key_env") or "GX10_SEARCH_API_KEY"), "")
        if not key:
            return UnavailableAdapter("native", "the search API key is not set in the environment")
        from websearch_brave import BraveAdapter
        try:                                   # never raise out of the builder on a bad config value
            count = int(search.get("count", 10) or 10)
        except (TypeError, ValueError):
            count = 10
        return BraveAdapter(key, count=count)
    return CliDelegateAdapter(dispatcher)
