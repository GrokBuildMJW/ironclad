"""Advisory Python source scan for egress-capable imports and calls.

This is a fast pre-filter, not a proof. Dynamic imports, importlib use, and
obfuscation are intentionally out of scope. Third-party import-name coverage is
best-effort and separate from dependency distribution-name analysis.
"""
from __future__ import annotations

import ast
import shlex
from pathlib import Path

from . import rust_scan

SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}

STDLIB_NETWORK_MODULES = {
    "ftplib": "stdlib network module import",
    "http.client": "stdlib network module import",
    "http.server": "stdlib network server module import",
    "imaplib": "stdlib network module import",
    "poplib": "stdlib network module import",
    "smtplib": "stdlib network module import",
    "socket": "stdlib raw socket module import",
    "socketserver": "stdlib socket server module import",
    "ssl": "stdlib TLS/network module import",
    "telnetlib": "stdlib network module import",
    "urllib.request": "stdlib URL opener module import",
    "urllib3": "egress-capable HTTP import",
    "xmlrpc.client": "stdlib network RPC module import",
}

THIRD_PARTY_EGRESS_IMPORTS = {
    "aiohttp": "known egress-capable third-party import",
    "boto3": "known egress-capable third-party import",
    "botocore": "known egress-capable third-party import",
    "grpc": "known egress-capable third-party import",
    "httpx": "known egress-capable third-party import",
    "paramiko": "known egress-capable third-party import",
    "pika": "known egress-capable third-party import",
    "requests": "known egress-capable third-party import",
    "socketio": "known egress-capable third-party import",
    "websockets": "known egress-capable third-party import",
}

NETWORK_CALLS = {
    "asyncio.open_connection": "asyncio stream opener",
    "asyncio.start_server": "asyncio stream server opener",
    "socket.create_connection": "raw socket connection",
    "socket.socket": "raw socket construction",
    "urllib.request.urlopen": "URL opener call",
}

SHELL_NETWORK_TOOLS = {"curl", "ftp", "nc", "ncat", "scp", "ssh", "telnet", "wget"}
SHELL_CALLS = {"os.popen", "os.system", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.run", "subprocess.Popen"}


def scan_source_tree(project_root: Path) -> dict:
    """Scan Python files under ``project_root`` for advisory egress affordances.

    The scanner is fail-soft: invalid roots, unreadable files, and syntax errors
    are skipped. Only successfully parsed files are counted.
    """
    try:
        root = Path(project_root)
    except Exception:
        return {"findings": [], "files_scanned": 0}
    python_result = _scan_python_source_tree(root)
    if not _has_rust_source(root):
        return python_result

    rust_result = _scan_rust_source_tree(root)
    return {
        "findings": list(python_result.get("findings") or []) + list(rust_result.get("findings") or []),
        "files_scanned": int(python_result.get("files_scanned") or 0) + int(rust_result.get("files_scanned") or 0),
    }


def _scan_python_source_tree(project_root: Path) -> dict:
    findings: list[dict] = []
    files_scanned = 0
    try:
        root = Path(project_root)
        paths = _python_files(root)
    except Exception:
        return {"findings": [], "files_scanned": 0}

    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except Exception:
            continue
        files_scanned += 1
        try:
            rel = path.relative_to(root).as_posix()
        except Exception:
            rel = path.as_posix()
        findings.extend(_scan_tree(tree, rel))

    findings.sort(key=lambda item: (item["file"], item["line"], item["symbol"]))
    return {"findings": findings, "files_scanned": files_scanned}


def _scan_rust_source_tree(project_root: Path) -> dict:
    try:
        return rust_scan.scan_rust_source_tree(project_root)
    except Exception:
        return {
            "findings": [{
                "severity": "advisory",
                "ecosystem": "rust",
                "reason": "rust egress analysis skipped (internal)",
            }],
            "files_scanned": 0,
        }


def _has_rust_source(project_root: Path) -> bool:
    try:
        root = Path(project_root)
        return any(path.is_file() for path in root.rglob("*.rs") if not _is_skipped(path, root))
    except Exception:
        return False


def _python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.py") if path.is_file() and not _is_skipped(path, root))


def _is_skipped(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except Exception:
        parts = path.parts
    return any(part in SKIP_DIRS for part in parts)


def _scan_tree(tree: ast.AST, filename: str) -> list[dict]:
    findings: list[dict] = []
    aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported = alias.name
                local = alias.asname or imported.split(".", 1)[0]
                aliases[local] = imported if alias.asname else local
                _add_import_finding(findings, filename, node, imported)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                full = f"{module}.{alias.name}" if module else alias.name
                aliases[alias.asname or alias.name] = full
            if module:
                _add_import_finding(findings, filename, node, module)
        elif isinstance(node, ast.Call):
            symbol = _call_symbol(node.func, aliases)
            if symbol in NETWORK_CALLS:
                findings.append(_finding(filename, node, symbol, NETWORK_CALLS[symbol]))
            shell_tool = _shell_network_tool(symbol, node)
            if shell_tool:
                findings.append(_finding(filename, node, f"subprocess:{shell_tool}", "shell-out to network tool"))

    return findings


def _add_import_finding(findings: list[dict], filename: str, node: ast.AST, imported: str) -> None:
    module = _known_module(imported, STDLIB_NETWORK_MODULES)
    if module:
        findings.append(_finding(filename, node, f"import {module}", STDLIB_NETWORK_MODULES[module]))
        return
    root = imported.split(".", 1)[0]
    if root in THIRD_PARTY_EGRESS_IMPORTS:
        findings.append(_finding(filename, node, f"import {root}", THIRD_PARTY_EGRESS_IMPORTS[root]))


def _known_module(imported: str, known: dict[str, str]) -> str:
    for module in sorted(known, key=len, reverse=True):
        if imported == module or imported.startswith(f"{module}."):
            return module
    return ""


def _call_symbol(node: ast.AST, aliases: dict[str, str]) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    else:
        return ""
    parts.reverse()
    if not parts:
        return ""
    first = aliases.get(parts[0], parts[0])
    return ".".join([first, *parts[1:]])


def _shell_network_tool(symbol: str, node: ast.Call) -> str:
    if symbol not in SHELL_CALLS or not node.args:
        return ""
    token = _first_command_token(node.args[0])
    if token in SHELL_NETWORK_TOOLS:
        return token
    return ""


def _first_command_token(node: ast.AST) -> str:
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        first = node.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return Path(first.value).name.lower()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            tokens = shlex.split(node.value, posix=True)
        except ValueError:
            return ""
        if tokens:
            return Path(tokens[0]).name.lower()
    return ""


def _finding(filename: str, node: ast.AST, symbol: str, reason: str) -> dict:
    return {
        "file": filename,
        "line": int(getattr(node, "lineno", 0) or 0),
        "symbol": symbol,
        "reason": reason,
        "severity": "advisory",
    }


__all__ = ["scan_source_tree"]
