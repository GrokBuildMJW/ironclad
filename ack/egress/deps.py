"""Python dependency-closure tripwire for egress-capable packages."""
from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Callable, Iterable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: pyproject parsing degrades fail-soft.
    tomllib = None

from .known_egress import KNOWN_EGRESS_DISTS
from . import rust_deps

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_CANON_RE = re.compile(r"[-_.]+")
_PIN_RE = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?\s*==\s*[^;\s#]+")
_SPEC_RE = re.compile(r"\s*(?:===|==|~=|!=|<=|>=|<|>|@)\s*")


def canonicalize_name(name: object) -> str:
    """Return the PEP 503 canonical distribution name, or an empty string."""
    try:
        text = str(name).strip()
    except Exception:
        return ""
    if not text:
        return ""
    return _CANON_RE.sub("-", text).lower()


def resolve_closure(project_root: Path) -> tuple[set[str], bool]:
    """Resolve local Python dependency names and whether they are a full closure.

    A fully pinned ``requirements.txt`` is treated as the committed lockfile for
    v1. Other requirement files and Python manifests are direct/best-effort only.
    The resolver is fail-soft and never raises.
    """
    try:
        root = Path(project_root)
    except Exception:
        return set(), False

    try:
        req_files = sorted(p for p in root.glob("requirements*.txt") if p.is_file())
        if req_files:
            names: set[str] = set()
            all_pinned = True
            for path in req_files:
                parsed, pinned = _parse_requirements(path)
                names.update(parsed)
                all_pinned = all_pinned and pinned
            if names:
                return names, bool(all_pinned)

        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            names = _parse_pyproject(pyproject)
            if names:
                return names, False

        setup_cfg = root / "setup.cfg"
        if setup_cfg.is_file():
            names = _parse_setup_cfg(setup_cfg)
            if names:
                return names, False

        setup_py = root / "setup.py"
        if setup_py.is_file():
            return _parse_setup_py(setup_py), False
    except Exception:
        return set(), False

    return set(), False


def analyze_dependencies(
    project_root: Path,
    policy: dict,
    *,
    rust_feature_resolver: Callable[[Path], dict | None] | None = None,
) -> dict:
    """Analyze local dependencies against an egress policy without raising."""
    try:
        root = Path(project_root)
    except Exception:
        return {"findings": [], "is_full_closure": False, "network": "open", "closure_size": 0}
    python_result = _analyze_python_dependencies(root, policy)
    if not _has_cargo_manifest(root):
        return python_result

    rust_result = _analyze_rust_dependencies(root, policy, rust_feature_resolver)
    has_python = _has_python_manifest(root)
    ran_results = [rust_result]
    if has_python:
        ran_results.insert(0, python_result)

    return {
        "findings": list(python_result.get("findings") or []) + list(rust_result.get("findings") or []),
        "is_full_closure": all(bool(result.get("is_full_closure")) for result in ran_results),
        "network": python_result.get("network", rust_result.get("network", "open")),
        "closure_size": sum(int(result.get("closure_size") or 0) for result in ran_results),
    }


def _analyze_python_dependencies(project_root: Path, policy: dict) -> dict:
    try:
        closure, is_full_closure = resolve_closure(project_root)
        policy = policy if isinstance(policy, dict) else {}
        network = str(policy.get("network", "open")).strip().lower()
        if network not in {"none", "declared", "open"}:
            network = "open"
        allow = _canonical_set(policy.get("allow", []))
        deny = _canonical_set(policy.get("deny", []))

        findings = []
        seen: set[tuple[str, str]] = set()

        for package in sorted(closure & deny):
            _add_finding(findings, seen, package, "explicitly denied by egress policy", "block")

        known = closure & KNOWN_EGRESS_DISTS
        if network == "none":
            for package in sorted(known - allow):
                _add_finding(
                    findings,
                    seen,
                    package,
                    "known egress-capable dependency is not allow-listed for network:none",
                    "block",
                )
        elif network == "declared":
            for package in sorted(known - allow):
                _add_finding(
                    findings,
                    seen,
                    package,
                    "known egress-capable dependency under network:declared",
                    "advisory",
                )

        return {
            "findings": findings,
            "is_full_closure": is_full_closure,
            "network": network,
            "closure_size": len(closure),
        }
    except Exception:
        return {"findings": [], "is_full_closure": False, "network": "open", "closure_size": 0}


def _analyze_rust_dependencies(
    project_root: Path,
    policy: dict,
    rust_feature_resolver: Callable[[Path], dict | None] | None,
) -> dict:
    try:
        rust_policy = rust_deps.select_ecosystem_policy(policy if isinstance(policy, dict) else {}, "rust")
        active_features = rust_feature_resolver(project_root) if rust_feature_resolver else None
        return rust_deps.analyze_rust_dependencies(project_root, rust_policy, active_features=active_features)
    except Exception:
        network = str((policy if isinstance(policy, dict) else {}).get("network", "open")).strip().lower()
        if network not in {"none", "declared", "open"}:
            network = "open"
        return {
            "findings": [{
                "severity": "advisory",
                "ecosystem": "rust",
                "reason": "rust egress analysis skipped (internal)",
            }],
            "is_full_closure": False,
            "network": network,
            "closure_size": 0,
        }


def _has_cargo_manifest(project_root: Path) -> bool:
    try:
        return (Path(project_root) / "Cargo.toml").is_file()
    except Exception:
        return False


def _has_python_manifest(project_root: Path) -> bool:
    try:
        root = Path(project_root)
        return (
            any(path.is_file() for path in root.glob("requirements*.txt"))
            or (root / "pyproject.toml").is_file()
            or (root / "setup.py").is_file()
            or (root / "setup.cfg").is_file()
        )
    except Exception:
        return False


def _add_finding(findings: list[dict], seen: set[tuple[str, str]], package: str, reason: str, severity: str) -> None:
    key = (package, reason)
    if key in seen:
        return
    findings.append({"package": package, "reason": reason, "severity": severity})
    seen.add(key)


def _canonical_set(values: object) -> set[str]:
    if isinstance(values, str):
        values = re.split(r"[\s,]+", values)
    if not isinstance(values, Iterable):
        return set()
    return {name for value in values if (name := canonicalize_name(value))}


def _parse_requirements(path: Path) -> tuple[set[str], bool]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return set(), False

    names: set[str] = set()
    all_pinned = True
    saw_requirement = False
    for raw in lines:
        line = _strip_requirement_comment(raw).strip()
        if not line:
            continue
        if _is_hash_continuation(line):
            continue
        name = "" if _is_standalone_non_package_requirement(line) else _name_from_requirement(line)
        is_pinned = bool(_PIN_RE.match(line))
        is_plain_pinned = bool(name and is_pinned and not _is_non_closure_requirement(line))
        if name:
            saw_requirement = True
            names.add(name)
        if not is_plain_pinned:
            all_pinned = False
            continue
    return names, bool(saw_requirement and all_pinned)


def _is_hash_continuation(line: str) -> bool:
    return line.strip().lower().startswith("--hash=")


def _is_non_closure_requirement(line: str) -> bool:
    text = line.strip()
    lower = text.lower()
    if _is_standalone_non_package_requirement(text):
        return True
    if "://" in lower:
        return True
    return bool(re.search(r"\s@\s*(?:git\+)?(?:https?://|file:)", lower))


def _is_standalone_non_package_requirement(line: str) -> bool:
    lower = line.strip().lower()
    return lower.startswith((
        "-",
        "git+",
        "http://",
        "https://",
        "file:",
    ))


def _strip_requirement_comment(line: str) -> str:
    in_quote = ""
    for index, char in enumerate(line):
        if char in {"'", '"'}:
            in_quote = "" if in_quote == char else char
        elif char == "#" and not in_quote:
            return line[:index]
    return line


def _name_from_requirement(requirement: object) -> str:
    try:
        text = str(requirement).strip()
    except Exception:
        return ""
    if not text:
        return ""
    text = text.split(";", 1)[0].strip()
    text = re.split(r"\s+--", text, maxsplit=1)[0].strip()
    text = _SPEC_RE.split(text, maxsplit=1)[0].strip()
    text = text.split("[", 1)[0].strip()
    match = _NAME_RE.match(text)
    return canonicalize_name(match.group(1)) if match else ""


def _parse_pyproject(path: Path) -> set[str]:
    if tomllib is None:
        return set()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    project = data.get("project")
    if not isinstance(project, dict):
        return set()
    names: set[str] = set()
    names.update(_names_from_requirements(project.get("dependencies", [])))
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for values in optional.values():
            names.update(_names_from_requirements(values))
    return names


def _parse_setup_cfg(path: Path) -> set[str]:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except Exception:
        return set()
    names: set[str] = set()
    if parser.has_option("options", "install_requires"):
        names.update(_names_from_requirements(parser.get("options", "install_requires").splitlines()))
    if parser.has_section("options.extras_require"):
        for _, value in parser.items("options.extras_require"):
            names.update(_names_from_requirements(value.splitlines()))
    return names


def _parse_setup_py(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return set()
    names: set[str] = set()
    for key in ("install_requires", "requires"):
        names.update(_names_from_literal_lists(text, key))
    return names


def _names_from_literal_lists(text: str, key: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(rf"{re.escape(key)}\s*=\s*\[([^\]]*)\]", text, re.DOTALL):
        for item in re.finditer(r"""['"]([^'"]+)['"]""", match.group(1)):
            name = _name_from_requirement(item.group(1))
            if name:
                names.add(name)
    return names


def _names_from_requirements(values: object) -> set[str]:
    if isinstance(values, str):
        values = values.splitlines()
    if not isinstance(values, Iterable):
        return set()
    names: set[str] = set()
    for value in values:
        name = _name_from_requirement(value)
        if name:
            names.add(name)
    return names


__all__ = ["analyze_dependencies", "canonicalize_name", "resolve_closure"]
