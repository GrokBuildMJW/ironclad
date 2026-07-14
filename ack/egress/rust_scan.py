"""Advisory Rust source scan for egress-capable imports and calls.

This is a fast pre-filter, not a proof. Rust is scanned with regular
expressions over raw source text instead of an AST, so comments and string
literals can match and dynamic/non-literal affordances are intentionally out of
scope.
"""
from __future__ import annotations

import re
from pathlib import Path

from .rust_known_egress import KNOWN_EGRESS_CRATES

try:
    from .staticscan import SHELL_NETWORK_TOOLS
except Exception:
    SHELL_NETWORK_TOOLS = {"curl", "ftp", "nc", "ncat", "scp", "ssh", "telnet", "wget"}

SKIP_DIRS = {
    ".git",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "target",
    "vendor",
    "vendored",
    "venv",
}

STD_NET_TYPES = {
    "TcpListener": "std::net listener affordance",
    "TcpStream": "std::net stream affordance",
    "UdpSocket": "std::net UDP socket affordance",
}
STD_NET_CALLS = {
    "TcpListener::bind": "std::net listener bind call",
    "TcpStream::connect": "std::net stream connect call",
    "UdpSocket::bind": "std::net UDP socket bind call",
}
KNOWN_EGRESS_CRATE_IDENTIFIERS = frozenset(crate.replace("-", "_") for crate in KNOWN_EGRESS_CRATES)

_USE_STD_NET_RE = re.compile(r"^\s*use\s+std::net(?:::|\s*::\s*)")
_STD_NET_BRACES_RE = re.compile(r"\{([^}]*)\}")
_STD_NET_CALL_RE = re.compile(r"\b(TcpListener::bind|TcpStream::connect|UdpSocket::bind)\b")
_COMMAND_NEW_RE = re.compile(r'\b(?:std::process::)?Command::new\s*\(\s*"([^"]+)"')


def scan_rust_source_tree(project_root: Path) -> dict:
    """Scan Rust files under ``project_root`` for advisory egress affordances.

    The scanner is fail-soft: invalid roots, unreadable files, and undecodable
    files are skipped. Only successfully read files are counted.
    """
    findings: list[dict] = []
    files_scanned = 0
    try:
        root = Path(project_root)
        paths = _rust_files(root)
    except Exception:
        return {"findings": [], "files_scanned": 0}

    crate_pattern = _known_crate_pattern()
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except Exception:
            continue
        files_scanned += 1
        try:
            rel = path.relative_to(root).as_posix()
        except Exception:
            rel = path.as_posix()
        findings.extend(_scan_source(source, rel, crate_pattern))

    findings.sort(key=lambda item: (item["file"], item["line"], item["symbol"]))
    return {"findings": findings, "files_scanned": files_scanned}


def _rust_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.rs") if path.is_file() and not _is_skipped(path, root))


def _is_skipped(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except Exception:
        parts = path.parts
    return any(part in SKIP_DIRS for part in parts)


def _scan_source(source: str, filename: str, crate_pattern: re.Pattern[str]) -> list[dict]:
    findings: list[dict] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        findings.extend(_scan_std_net(line, filename, lineno))
        findings.extend(_scan_known_crates(line, filename, lineno, crate_pattern))
        findings.extend(_scan_command_new(line, filename, lineno))
    return findings


def _scan_std_net(line: str, filename: str, lineno: int) -> list[dict]:
    findings: list[dict] = []
    if _USE_STD_NET_RE.search(line):
        imported = _std_net_imports(line)
        symbols = imported if imported else ["std::net"]
        for symbol in symbols:
            findings.append(_finding(filename, lineno, f"use std::net::{symbol}", "std::net import"))
    for match in _STD_NET_CALL_RE.finditer(line):
        symbol = match.group(1)
        findings.append(_finding(filename, lineno, symbol, STD_NET_CALLS[symbol]))
    return findings


def _std_net_imports(line: str) -> list[str]:
    brace_match = _STD_NET_BRACES_RE.search(line)
    if brace_match:
        names = []
        for item in brace_match.group(1).split(","):
            name = item.strip().split(" as ", 1)[0].strip()
            if name in STD_NET_TYPES:
                names.append(name)
        return names
    for name in STD_NET_TYPES:
        if re.search(rf"\b{name}\b", line):
            return [name]
    return []


def _scan_known_crates(line: str, filename: str, lineno: int, crate_pattern: re.Pattern[str]) -> list[dict]:
    findings: list[dict] = []
    for match in crate_pattern.finditer(line):
        crate = match.group("crate")
        symbol = f"use {crate}" if match.group("use") else f"{crate}::"
        findings.append(
            _finding(filename, lineno, symbol, "known egress-capable crate use")
        )
    return findings


def _scan_command_new(line: str, filename: str, lineno: int) -> list[dict]:
    findings: list[dict] = []
    for match in _COMMAND_NEW_RE.finditer(line):
        tool = Path(match.group(1)).name.lower()
        if tool in SHELL_NETWORK_TOOLS:
            findings.append(_finding(filename, lineno, tool, "shell-out to network tool"))
    return findings


def _known_crate_pattern() -> re.Pattern[str]:
    crates = "|".join(re.escape(crate) for crate in sorted(KNOWN_EGRESS_CRATE_IDENTIFIERS, key=len, reverse=True))
    return re.compile(rf"(?P<use>\buse\s+)?(?P<crate>{crates})(?=\s*(?:::|;|\{{|\bas\b))")


def _finding(filename: str, line: int, symbol: str, reason: str) -> dict:
    return {
        "file": filename,
        "line": line,
        "symbol": symbol,
        "reason": reason,
        "severity": "advisory",
        "ecosystem": "rust",
    }


__all__ = ["scan_rust_source_tree"]
