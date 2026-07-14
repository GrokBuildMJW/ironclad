"""Rust dependency-closure tripwire for egress-capable Cargo crates."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: Cargo manifest parsing degrades fail-soft.
    tomllib = None

from .rust_known_egress import KNOWN_EGRESS_CRATES

_CANON_RE = re.compile(r"[-_]+")
_DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")

# Feature-gated crates are not in KNOWN_EGRESS_CRATES because they are common
# infrastructure crates whose egress capability depends on enabled features.
# tokio: socket APIs are behind net; full includes net.
# git2: HTTPS and SSH transports enable remote repository access.
# async-std: default includes networking APIs; unstable exposes additional APIs.
FEATURE_GATED_EGRESS = {
    "async-std": {"default", "unstable"},
    "git2": {"https", "ssh"},
    "tokio": {"net", "full"},
}


def canonicalize_crate(name: object) -> str:
    """Return the Cargo canonical crate name, or an empty string."""
    try:
        text = str(name).strip()
    except Exception:
        return ""
    if not text:
        return ""
    return _CANON_RE.sub("-", text).lower()


def resolve_rust_closure(project_root: Path) -> tuple[set[str], bool]:
    """Resolve local Cargo dependency names and whether they are a full closure.

    ``Cargo.lock`` is authoritative because its ``[[package]]`` entries contain
    the full transitive closure. Without it, direct Cargo manifest dependencies
    are returned as best-effort only. The resolver is fail-soft and never raises.
    """
    if tomllib is None:
        return set(), False
    try:
        root = Path(project_root)
    except Exception:
        return set(), False

    try:
        lockfile = root / "Cargo.lock"
        if lockfile.is_file():
            return _parse_cargo_lock(lockfile), True

        manifest = root / "Cargo.toml"
        if manifest.is_file():
            return _parse_cargo_manifest(manifest), False
    except Exception:
        return set(), False

    return set(), False


def analyze_rust_dependencies(
    project_root: Path,
    policy: dict,
    *,
    active_features: Optional[dict] = None,
) -> dict:
    """Analyze local Cargo dependencies against an egress policy without raising."""
    try:
        closure, is_full_closure = resolve_rust_closure(project_root)
        selected = select_ecosystem_policy(policy, "rust")
        network = selected["network"]
        allow = selected["allow"]
        deny = selected["deny"]

        findings = []
        seen: set[tuple[str, str]] = set()

        for package in sorted(closure & deny):
            _add_finding(findings, seen, package, "explicitly denied by egress policy", "block")

        known = closure & KNOWN_EGRESS_CRATES
        if network == "none":
            for package in sorted(known - allow):
                _add_finding(
                    findings,
                    seen,
                    package,
                    "known egress-capable crate is not allow-listed for network:none",
                    "block",
                )
        elif network == "declared":
            for package in sorted(known - allow):
                _add_finding(
                    findings,
                    seen,
                    package,
                    "known egress-capable crate under network:declared",
                    "advisory",
                )

        _add_feature_gated_findings(findings, seen, closure, allow, network, active_features)

        return {
            "findings": findings,
            "is_full_closure": is_full_closure,
            "network": network,
            "closure_size": len(closure),
        }
    except Exception:
        return {"findings": [], "is_full_closure": False, "network": "open", "closure_size": 0}


def select_ecosystem_policy(policy: dict, ecosystem: str) -> dict:
    """Select an ecosystem-specific egress policy from namespaced allow/deny entries."""
    try:
        source = policy if isinstance(policy, dict) else {}
        network = str(source.get("network", "open")).strip().lower()
        if network not in {"none", "declared", "open"}:
            network = "open"
        prefix = f"{str(ecosystem).strip().lower()}:"
        return {
            "network": network,
            "allow": _select_names(source.get("allow", []), prefix),
            "deny": _select_names(source.get("deny", []), prefix),
        }
    except Exception:
        return {"network": "open", "allow": set(), "deny": set()}


def _add_feature_gated_findings(
    findings: list[dict],
    seen: set[tuple[str, str]],
    closure: set[str],
    allow: set[str],
    network: str,
    active_features: Optional[dict],
) -> None:
    for package in sorted(closure & set(FEATURE_GATED_EGRESS)):
        enabled = _active_feature_set(active_features, package) if active_features is not None else None
        egress_features = FEATURE_GATED_EGRESS[package]
        has_egress_feature = bool(enabled is not None and enabled & egress_features)
        if has_egress_feature:
            if network == "none" and package not in allow:
                _add_finding(
                    findings,
                    seen,
                    package,
                    "egress-capable feature is enabled and is not allow-listed for network:none",
                    "block",
                )
            elif network == "declared" and package not in allow:
                _add_finding(
                    findings,
                    seen,
                    package,
                    "egress-capable feature is enabled under network:declared",
                    "advisory",
                )
            continue

        reason = (
            "feature-gated egress crate; features not resolved"
            if enabled is None
            else "feature-gated egress crate; egress feature not enabled"
        )
        _add_finding(findings, seen, package, reason, "advisory")


def _active_feature_set(active_features: object, package: str) -> set[str] | None:
    if not isinstance(active_features, dict):
        return set()
    values = active_features.get(package)
    if values is None:
        values = active_features.get(package.replace("-", "_"))
    if values is None:
        return set()
    return _canonical_feature_set(values)


def _add_finding(findings: list[dict], seen: set[tuple[str, str]], package: str, reason: str, severity: str) -> None:
    key = (package, reason)
    if key in seen:
        return
    findings.append({"package": package, "reason": reason, "severity": severity, "ecosystem": "rust"})
    seen.add(key)


def _select_names(values: object, ecosystem_prefix: str) -> set[str]:
    names: set[str] = set()
    for raw in _iter_policy_values(values):
        text = str(raw).strip()
        lower = text.lower()
        if ":" in lower:
            prefix, name = lower.split(":", 1)
            if f"{prefix}:" != ecosystem_prefix:
                continue
            text = name
        name = canonicalize_crate(text)
        if name:
            names.add(name)
    return names


def _iter_policy_values(values: object) -> Iterable[object]:
    if isinstance(values, str):
        return re.split(r"[\s,]+", values)
    if isinstance(values, Iterable):
        return values
    return ()


def _parse_cargo_lock(path: Path) -> set[str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    packages = data.get("package", [])
    if not isinstance(packages, list):
        return set()
    return {
        name
        for package in packages
        if isinstance(package, dict) and (name := canonicalize_crate(package.get("name", "")))
    }


def _parse_cargo_manifest(path: Path) -> set[str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    names: set[str] = set()
    for table_name in _DEPENDENCY_TABLES:
        names.update(_dependency_names_from_table(data.get(table_name)))
    return names


def _dependency_names_from_table(table: object) -> set[str]:
    if not isinstance(table, dict):
        return set()
    names: set[str] = set()
    for key, value in table.items():
        package_name = ""
        if isinstance(value, dict):
            package_name = canonicalize_crate(value.get("package", ""))
        names.add(package_name or canonicalize_crate(key))
    return {name for name in names if name}


def _canonical_feature_set(values: object) -> set[str]:
    if isinstance(values, str):
        values = re.split(r"[\s,]+", values)
    if not isinstance(values, Iterable):
        return set()
    return {name for value in values if (name := canonicalize_crate(value))}


__all__ = [
    "FEATURE_GATED_EGRESS",
    "analyze_rust_dependencies",
    "canonicalize_crate",
    "resolve_rust_closure",
    "select_ecosystem_policy",
]
