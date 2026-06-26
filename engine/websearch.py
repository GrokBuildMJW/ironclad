"""Pure web-search input contract + domain normalizer (epic #505, S1).

Standalone and synchronous: this module imports **nothing** from the engine, does
**no** network I/O, and adds **no** runtime dependency (standard library only). It is the
single home for the strict web-search input rules.

Why the rules live here and not in the model-facing JSON tool schema: the structured-
outputs path (XGrammar V1) rejects ``minLength``/``pattern``/``minItems``, so the tool
schema must stay grammar-clean. The strict rules are therefore enforced imperatively at
the tool boundary — the Validate->Reask contract, mirroring
``gx10._parse_tool_args``: on a violation we return a short, model-readable error string
so the model re-emits the call instead of us silently swallowing it.

Public surface:
    * ``validate_web_search_input(args)`` -> ``(WebSearchRequest | None, error | None)``
    * ``normalize_domain(value)`` -> a bare, lowercased host
    * ``has_wildcard(value)`` -> bool (the only wildcard surface we reject; see epic #505
      Fork 4 — Ironclad has no per-tool permission layer, so the meaningful wildcard reject
      is on the domain filters, not on a permission pattern)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

#: Minimum number of non-whitespace characters a query must contain.
MIN_QUERY_LEN = 2

# A leading URL scheme, e.g. ``https://`` or ``ftp://`` (RFC-3986 shape, kept liberal).
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
# Glob/permission wildcards we do not support in a concrete domain filter.
_WILDCARD_RE = re.compile(r"[*?]")


@dataclass(frozen=True)
class WebSearchRequest:
    """A validated, normalized web-search request. ``allow_domains`` and ``block_domains``
    are mutually exclusive (at most one is non-empty) and already normalized."""

    query: str
    allow_domains: Tuple[str, ...] = ()
    block_domains: Tuple[str, ...] = ()


def has_wildcard(value: str) -> bool:
    """True if *value* contains a ``*`` or ``?`` wildcard (which we reject in domains)."""
    return bool(_WILDCARD_RE.search(value))


def normalize_domain(value: str) -> str:
    """Reduce *value* to a bare host: drop any URL scheme and path, trim, lowercase.

    ``"HTTPS://Foo.com/Path?x=1"`` -> ``"foo.com"``. Pure; never raises.
    """
    host = _SCHEME_RE.sub("", value.strip())
    host = host.split("/", 1)[0]          # drop path
    host = host.split("?", 1)[0]          # drop a path-less query string
    return host.strip().lower()


def _normalize_domain_list(values: Any, label: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Normalize one domain filter list, or return a reask error string."""
    if not isinstance(values, list):
        return None, (f"web_search '{label}' must be an array of domain strings; "
                      "re-emit with a list or omit it.")
    out: List[str] = []
    for raw in values:
        if not isinstance(raw, str):
            return None, (f"web_search '{label}' entries must be strings, "
                          f"got {type(raw).__name__}.")
        if has_wildcard(raw):
            return None, (f"web_search '{label}' does not support wildcards ('*'/'?'); "
                          "use a concrete domain such as 'example.com'.")
        host = normalize_domain(raw)
        if not host:
            return None, f"web_search '{label}' contains an empty or invalid domain: {raw!r}."
        out.append(host)
    return out, None


def validate_web_search_input(args: Any) -> Tuple[Optional[WebSearchRequest], Optional[str]]:
    """Validate and normalize raw web_search tool arguments.

    Returns ``(request, None)`` on success or ``(None, error)`` where *error* is a short,
    model-readable reask string. Rules:

    * ``query`` is a string with at least :data:`MIN_QUERY_LEN` non-whitespace characters;
    * ``allowDomains`` and ``blockDomains`` are mutually exclusive (set at most one);
    * each domain is normalized (no scheme, no path, lowercase) and wildcard-free.
    """
    if not isinstance(args, dict):
        return None, (f"web_search arguments must be a JSON object, "
                      f"got {type(args).__name__}.")

    query = args.get("query")
    if not isinstance(query, str) or len(query.strip()) < MIN_QUERY_LEN:
        return None, (f"web_search 'query' must be a string of at least {MIN_QUERY_LEN} "
                      "characters; re-emit with a longer query.")

    allow = args.get("allowDomains") or []
    block = args.get("blockDomains") or []
    if allow and block:
        return None, ("web_search 'allowDomains' and 'blockDomains' are mutually exclusive; "
                      "set only one and re-emit.")

    norm_allow, err = _normalize_domain_list(allow, "allowDomains")
    if err:
        return None, err
    norm_block, err = _normalize_domain_list(block, "blockDomains")
    if err:
        return None, err

    return WebSearchRequest(
        query=query.strip(),
        allow_domains=tuple(norm_allow or ()),
        block_domains=tuple(norm_block or ()),
    ), None
