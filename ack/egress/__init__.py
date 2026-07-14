"""Egress tripwire helpers."""
from __future__ import annotations

from .deps import analyze_dependencies, canonicalize_name, resolve_closure
from .known_egress import KNOWN_EGRESS_DISTS, KNOWN_EGRESS_VERSION
from .rust_deps import (
    FEATURE_GATED_EGRESS,
    analyze_rust_dependencies,
    canonicalize_crate,
    resolve_rust_closure,
    select_ecosystem_policy,
)
from .rust_known_egress import KNOWN_EGRESS_CRATES, KNOWN_EGRESS_CRATES_VERSION
from .rust_scan import scan_rust_source_tree
from .staticscan import scan_source_tree

__all__ = [
    "FEATURE_GATED_EGRESS",
    "KNOWN_EGRESS_CRATES",
    "KNOWN_EGRESS_CRATES_VERSION",
    "KNOWN_EGRESS_DISTS",
    "KNOWN_EGRESS_VERSION",
    "analyze_dependencies",
    "analyze_rust_dependencies",
    "canonicalize_name",
    "canonicalize_crate",
    "resolve_closure",
    "resolve_rust_closure",
    "scan_rust_source_tree",
    "scan_source_tree",
    "select_ecosystem_policy",
]
