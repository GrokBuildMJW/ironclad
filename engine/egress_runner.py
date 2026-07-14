"""Sandboxed build/test hermeticity probe for Python projects.

This engine-side runner contains the BUILD/TEST step under the ADR-0013 sandbox; it does NOT contain the
delivered product, which the end user runs outside this build-time sandbox. It also trusts a coder-written
test suite. The result is therefore a strong tripwire for blatant drift (a build/test that needs a forbidden
network hard-fails), not a runtime-egress guarantee.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ack.egress.rust_deps import canonicalize_crate
from engine import sandbox

try:  # pragma: no cover - Python 3.11+ path
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

TIMEOUT_SECONDS = 120
RUST_TIMEOUT_SECONDS = 300
OUTPUT_TAIL_CHARS = 4000
_RUST_DROPPED_ENV = {
    "RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER",
    "RUSTC",
    "CARGO_BUILD_RUSTC",
    "CARGO_BUILD_RUSTC_WRAPPER",
    "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
    "RUSTFLAGS",
    "CARGO_BUILD_RUSTFLAGS",
    "CARGO",
    "RUSTUP_TOOLCHAIN",
}
_NET_DENY_SIGNATURES = [
    "Network is unreachable",
    "Temporary failure in name resolution",
    "Could not resolve host",
    "Connection refused",
    "Operation not permitted",
    "failed to lookup address",
    "Name or service not known",
    "no address associated with host",
    "Network is down",
    "nodename nor servname provided",
]


def _has_tests(project_root: Path) -> bool:
    try:
        if (project_root / "tests").is_dir():
            return True
        return any(p.is_file() and (p.name.startswith("test_") or p.name.endswith("_test.py"))
                   for p in project_root.rglob("*.py"))
    except Exception:  # noqa: BLE001
        return False


def discover_build_test(project_root: Path) -> list[list[str]]:
    """Best-effort Python build/test command discovery.

    Returns argv lists, never shell strings. The discovery is intentionally conservative: pytest is selected
    only when a test surface is visible, and the wheel build check is added only when project metadata exists
    and the ``build`` module is import-checkable in the current interpreter. Empty output means there is no
    build/test command to probe.
    """
    try:
        root = Path(project_root)
        commands: list[list[str]] = []
        if _has_tests(root):
            commands.append([sys.executable, "-m", "pytest", "-q"])
        if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
            if importlib.util.find_spec("build") is not None:
                commands.append([sys.executable, "-m", "build", "--wheel"])
        return commands
    except Exception:  # noqa: BLE001
        return []


def _command_string(argv: list[str]) -> str:
    return shlex.join([str(part) for part in argv])


def _tail(stdout: str, stderr: str) -> str:
    text = "\n".join(part for part in (stdout, stderr) if part)
    return text[-OUTPUT_TAIL_CHARS:]


def _run_command(command: str, project_root: Path, timeout: int = TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        command,
        shell=True,
        cwd=str(project_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _run_cargo(command: str, project_root: Path, env: dict[str, str],
               timeout: int = RUST_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        shell=True,
        cwd=str(project_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _finding(command: str, reason: str, severity: str, *,
             exit_code: int | None = None, output_tail: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"command": command, "reason": reason, "severity": severity}
    if exit_code is not None:
        out["exit_code"] = exit_code
    if output_tail:
        out["output_tail"] = output_tail
    return out


def _rust_finding(command: str, reason: str, severity: str, *,
                  exit_code: int | None = None, output_tail: str | None = None) -> dict[str, Any]:
    out = _finding(command, reason, severity, exit_code=exit_code, output_tail=output_tail)
    out["ecosystem"] = "rust"
    return out


def _has_cargo_manifest(project_root: Path) -> bool:
    try:
        return any(p.is_file() and p.name == "Cargo.toml" for p in Path(project_root).rglob("Cargo.toml"))
    except Exception:  # noqa: BLE001
        return False


def _walk_no_symlink_dirs(project_root: Path):
    for dirpath, dirnames, filenames in os.walk(project_root, followlinks=False):
        root = Path(dirpath)
        kept = []
        for dirname in dirnames:
            if not (root / dirname).is_symlink():
                kept.append(dirname)
        dirnames[:] = kept
        yield root, filenames


def _rust_config_paths(project_root: Path) -> list[Path] | None:
    try:
        configs = []
        for dirpath, dirnames, filenames in os.walk(Path(project_root), followlinks=False):
            root = Path(dirpath)
            if root.name == ".cargo" and root.is_symlink():
                return None
            for dirname in dirnames:
                path = root / dirname
                if dirname == ".cargo" and path.is_symlink():
                    return None
            kept = []
            for dirname in dirnames:
                if not (root / dirname).is_symlink():
                    kept.append(dirname)
            dirnames[:] = kept
            if root.name == ".cargo":
                for name in ("config", "config.toml"):
                    if name in filenames:
                        path = root / name
                        if path.is_symlink():
                            return None
                        if path.is_file():
                            configs.append(path)
        return configs
    except Exception:  # noqa: BLE001
        return None


def _has_key(data: object, key: str) -> bool:
    if isinstance(data, dict):
        for child_key, value in data.items():
            if child_key == key or _has_key(value, key):
                return True
    elif isinstance(data, list):
        return any(_has_key(value, key) for value in data)
    return False


def _rust_config_probe_safe(project_root: Path) -> bool:
    configs = _rust_config_paths(project_root)
    if configs is None:
        return False
    for config in configs:
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        include = data.get("include")
        if isinstance(include, str) or isinstance(include, list):
            return False
        if _has_key(data, "rustflags") or _has_key(data, "credential-provider"):
            return False
        build = data.get("build")
        if isinstance(build, dict) and any(k in build for k in ("rustc", "rustc-wrapper", "rustc-workspace-wrapper")):
            return False
        if isinstance(data.get("env"), dict):
            return False
        if isinstance(data.get("source"), dict):
            return False
        if "global-credential-providers" in data:
            return False
        registry = data.get("registry")
        if isinstance(registry, dict) and "global-credential-providers" in registry:
            return False
        target = data.get("target")
        if isinstance(target, dict):
            for value in target.values():
                if isinstance(value, dict) and ("runner" in value or "linker" in value):
                    return False
    return True


def _rust_toolchain_probe_safe(project_root: Path) -> bool:
    try:
        for root, filenames in _walk_no_symlink_dirs(Path(project_root)):
            for name in ("rust-toolchain", "rust-toolchain.toml"):
                if name not in filenames:
                    continue
                path = root / name
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                if path.name == "rust-toolchain.toml" or "[toolchain]" in text:
                    try:
                        data = tomllib.loads(text)
                    except Exception:  # noqa: BLE001
                        return False
                    toolchain = data.get("toolchain")
                    if isinstance(toolchain, dict) and "path" in toolchain:
                        return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _rust_env(cargo_home: str, cargo_target_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key in _RUST_DROPPED_ENV or (key.startswith("CARGO_TARGET_") and key.endswith("_RUNNER")):
            env.pop(key, None)
    env["CARGO_HOME"] = cargo_home
    env["CARGO_TARGET_DIR"] = cargo_target_dir
    env["PYTHONIOENCODING"] = "utf-8"
    env["RUSTUP_TOOLCHAIN"] = "stable"
    return env


def _has_net_deny_signature(text: str) -> bool:
    lower = text.lower()
    return any(signature.lower() in lower for signature in _NET_DENY_SIGNATURES)


def run_rust_hermetic(project_root: Path, *, network: str, sandbox_pref: str = "auto") -> dict:
    """Run the Rust two-phase hermetic build probe.

    The probe neutralizes Cargo configuration and environment code-exec vectors before the network-allowed
    fetch phase, then compiles under the ADR-0013 sandbox with ``--net=none``. It is intentionally fail-soft:
    only a clear sandbox network-denial signature blocks ``network:none``.
    """
    fetch_command = "cargo fetch --locked"
    build_commands = ["cargo build --frozen --offline", "cargo test --no-run --frozen --offline"]
    findings: list[dict[str, Any]] = []
    result = {"ran": False, "contained": False, "backend": "", "commands": build_commands, "findings": findings}

    try:
        root = Path(project_root)
        mode = (network or "").strip().lower()
        if mode == "open":
            result["commands"] = []
            return result
        if not _has_cargo_manifest(root):
            result["commands"] = []
            return result
        if shutil.which("cargo") is None:
            findings.append(_rust_finding("", "cargo not available - rust hermetic probe skipped", "advisory"))
            return result
        try:
            backend = sandbox.available_backend(sandbox_pref)
        except Exception:  # noqa: BLE001
            backend = ""
        result["backend"] = backend
        if not backend:
            findings.append(_rust_finding("", "no sandbox backend - rust egress containment not enforced here",
                                          "advisory"))
            return result
        if not _rust_config_probe_safe(root) or not _rust_toolchain_probe_safe(root):
            findings.append(_rust_finding("", "rust project config is not probe-safe - hermetic probe skipped",
                                          "advisory"))
            return result

        cargo_home = tempfile.mkdtemp(prefix="ironclad-rust-cargo-")
        cargo_target_dir = tempfile.mkdtemp(prefix="ironclad-rust-target-")
        try:
            env = _rust_env(cargo_home, cargo_target_dir)
            try:
                fetched = _run_cargo(fetch_command, root, env, RUST_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                findings.append(_rust_finding(fetch_command, "rust deps not fetchable - hermetic probe inconclusive",
                                              "advisory",
                                              output_tail=_tail(str(getattr(exc, "stdout", "") or ""),
                                                                str(getattr(exc, "stderr", "") or ""))))
                return result
            except Exception as exc:  # noqa: BLE001
                findings.append(_rust_finding(fetch_command, "rust deps not fetchable - hermetic probe inconclusive",
                                              "advisory", output_tail=str(exc)[-OUTPUT_TAIL_CHARS:]))
                return result
            if int(getattr(fetched, "returncode", 1)) != 0:
                findings.append(_rust_finding(fetch_command, "rust deps not fetchable - hermetic probe inconclusive",
                                              "advisory", exit_code=int(getattr(fetched, "returncode", 1)),
                                              output_tail=_tail(getattr(fetched, "stdout", ""),
                                                                getattr(fetched, "stderr", ""))))
                return result

            result["ran"] = True
            result["contained"] = True
            for command in build_commands:
                try:
                    wrapped = sandbox.wrap_command(command, backend=backend, net=False)
                    completed = _run_cargo(wrapped, root, env, RUST_TIMEOUT_SECONDS)
                    exit_code = int(getattr(completed, "returncode", 1))
                    if exit_code == 0:
                        continue
                    output_tail = _tail(getattr(completed, "stdout", ""), getattr(completed, "stderr", ""))
                    if _has_net_deny_signature(output_tail):
                        severity = "block" if mode == "none" else "advisory"
                        reason = f"rust build-time egress attempt blocked under --net=none: {command} - {output_tail}"
                        findings.append(_rust_finding(command, reason, severity, exit_code=exit_code,
                                                      output_tail=output_tail))
                    else:
                        findings.append(_rust_finding(command,
                                                      "rust hermetic probe inconclusive (build failed, no egress signature)",
                                                      "advisory", exit_code=exit_code, output_tail=output_tail))
                except subprocess.TimeoutExpired as exc:
                    findings.append(_rust_finding(command, "rust hermetic probe timed out", "advisory",
                                                  output_tail=_tail(str(getattr(exc, "stdout", "") or ""),
                                                                    str(getattr(exc, "stderr", "") or ""))))
                except Exception as exc:  # noqa: BLE001
                    findings.append(_rust_finding(command, f"rust hermetic probe failed: {exc}", "advisory"))
        finally:
            shutil.rmtree(cargo_home, ignore_errors=True)
            shutil.rmtree(cargo_target_dir, ignore_errors=True)
        return result
    except Exception as exc:  # noqa: BLE001
        findings.append(_rust_finding("", f"rust hermetic probe failed: {exc}", "advisory"))
        return result


def rust_feature_resolver(project_root: Path) -> dict[str, set[str]] | None:
    """Resolve active Cargo features with a neutralized offline metadata probe."""
    command = "cargo metadata --frozen --offline --format-version 1"
    try:
        root = Path(project_root)
        if not _has_cargo_manifest(root):
            return None
        if shutil.which("cargo") is None:
            return None
        if not _rust_config_probe_safe(root) or not _rust_toolchain_probe_safe(root):
            return None

        cargo_home = tempfile.mkdtemp(prefix="ironclad-rust-cargo-")
        cargo_target_dir = tempfile.mkdtemp(prefix="ironclad-rust-target-")
        try:
            completed = _run_cargo(command, root, _rust_env(cargo_home, cargo_target_dir), RUST_TIMEOUT_SECONDS)
            if int(getattr(completed, "returncode", 1)) != 0:
                return None
            data = json.loads(getattr(completed, "stdout", "") or "{}")
            packages = data.get("packages", [])
            package_names = {
                package.get("id"): canonicalize_crate(package.get("name", ""))
                for package in packages
                if isinstance(package, dict) and package.get("id")
            }
            resolved: dict[str, set[str]] = {}
            resolve = data.get("resolve")
            nodes = resolve.get("nodes", []) if isinstance(resolve, dict) else []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                crate = package_names.get(node.get("id"))
                if not crate:
                    continue
                features = node.get("features", [])
                if isinstance(features, list):
                    resolved[crate] = {canonicalize_crate(feature) for feature in features}
            return resolved
        finally:
            shutil.rmtree(cargo_home, ignore_errors=True)
            shutil.rmtree(cargo_target_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        return None


def run_hermetic(project_root: Path, *, network: str, sandbox_pref: str = "auto") -> dict:
    """Run per-ecosystem hermetic build/test probes without cross-ecosystem fail-open."""
    try:
        root = Path(project_root)
    except Exception:
        return {"ran": False, "contained": False, "backend": "", "commands": [], "findings": []}
    python_result = _run_python_hermetic(root, network=network, sandbox_pref=sandbox_pref)
    if not _has_cargo_manifest(root):
        return python_result

    try:
        rust_result = run_rust_hermetic(root, network=network, sandbox_pref=sandbox_pref)
    except Exception:
        rust_result = {
            "ran": False,
            "contained": False,
            "backend": "",
            "commands": [],
            "findings": [{
                "command": "",
                "reason": "rust egress analysis skipped (internal)",
                "severity": "advisory",
                "ecosystem": "rust",
            }],
        }

    ran_results = [result for result in (python_result, rust_result) if result.get("ran")]
    backend = str(python_result.get("backend") or rust_result.get("backend") or "")
    return {
        "ran": bool(python_result.get("ran") or rust_result.get("ran")),
        "contained": bool(ran_results) and all(bool(result.get("contained")) for result in ran_results),
        "backend": backend,
        "commands": list(python_result.get("commands") or []) + list(rust_result.get("commands") or []),
        "findings": list(python_result.get("findings") or []) + list(rust_result.get("findings") or []),
    }


def _run_python_hermetic(project_root: Path, *, network: str, sandbox_pref: str = "auto") -> dict:
    """Run discovered Python build/test commands under the sandbox when available.

    This contains the BUILD/TEST with ``--net=none`` for ``network:none``; it does not contain the delivered
    product and it relies on the coder-written tests that exist in the produced tree. Backend resolution is
    owned by this feature and is independent of ``security.sandbox``. The public API is fail-soft: internal
    errors, timeouts, and platform-no-backend cases return advisory findings instead of raising.
    """
    commands = discover_build_test(project_root)
    command_strings = [_command_string(cmd) for cmd in commands]
    findings: list[dict[str, Any]] = []
    result = {"ran": False, "contained": False, "backend": "", "commands": command_strings, "findings": findings}

    try:
        backend = sandbox.available_backend(sandbox_pref)
    except Exception:  # noqa: BLE001
        backend = ""
    result["backend"] = backend

    mode = (network or "").strip().lower()
    if mode == "none" and not backend:
        findings.append(_finding("", "no sandbox backend - egress containment not enforced here", "advisory"))
        return result
    if not commands:
        return result
    if not backend:
        findings.append(_finding("", "no sandbox backend - build/test probe skipped", "advisory"))
        return result

    result["ran"] = True
    result["contained"] = True
    for command in command_strings:
        try:
            wrapped = sandbox.wrap_command(command, backend=backend, net=False)
            completed = _run_command(wrapped, Path(project_root), TIMEOUT_SECONDS)
            exit_code = int(getattr(completed, "returncode", 1))
            if exit_code == 0:
                continue
            severity = "block" if mode == "none" else "advisory"
            reason = "build/test failed under denied network" if mode == "none" else "build/test failed in sandbox"
            findings.append(_finding(command, reason, severity, exit_code=exit_code,
                                     output_tail=_tail(getattr(completed, "stdout", ""),
                                                       getattr(completed, "stderr", ""))))
        except subprocess.TimeoutExpired as exc:
            findings.append(_finding(command, "build/test sandbox probe timed out", "advisory",
                                     output_tail=_tail(str(getattr(exc, "stdout", "") or ""),
                                                       str(getattr(exc, "stderr", "") or ""))))
        except Exception as exc:  # noqa: BLE001
            findings.append(_finding(command, f"build/test sandbox probe failed: {exc}", "advisory"))
    return result
