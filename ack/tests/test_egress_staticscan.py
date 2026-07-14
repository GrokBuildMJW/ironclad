from __future__ import annotations

from ack.egress import scan_source_tree


def _symbols(result):
    return [finding["symbol"] for finding in result["findings"]]


def test_socket_import_and_raw_socket_call_are_advisory_with_lines(tmp_path):
    (tmp_path / "net.py").write_text("import socket\n\nsock = socket.socket()\n", encoding="utf-8")

    result = scan_source_tree(tmp_path)

    assert result["files_scanned"] == 1
    assert result["findings"] == [
        {
            "file": "net.py",
            "line": 1,
            "symbol": "import socket",
            "reason": "stdlib raw socket module import",
            "severity": "advisory",
        },
        {
            "file": "net.py",
            "line": 3,
            "symbol": "socket.socket",
            "reason": "raw socket construction",
            "severity": "advisory",
        },
    ]


def test_third_party_import_and_urllib_alias_call_are_advisory(tmp_path):
    (tmp_path / "client.py").write_text(
        "import requests\nfrom urllib.request import urlopen\n\nurlopen('https://example.invalid')\n",
        encoding="utf-8",
    )

    result = scan_source_tree(tmp_path)

    assert result["findings"] == [
        {
            "file": "client.py",
            "line": 1,
            "symbol": "import requests",
            "reason": "known egress-capable third-party import",
            "severity": "advisory",
        },
        {
            "file": "client.py",
            "line": 2,
            "symbol": "import urllib.request",
            "reason": "stdlib URL opener module import",
            "severity": "advisory",
        },
        {
            "file": "client.py",
            "line": 4,
            "symbol": "urllib.request.urlopen",
            "reason": "URL opener call",
            "severity": "advisory",
        },
    ]


def test_dotted_stdlib_import_call_resolution_is_not_double_expanded(tmp_path):
    (tmp_path / "url.py").write_text(
        "import urllib.request\n\nurllib.request.urlopen('https://example.invalid')\n",
        encoding="utf-8",
    )

    result = scan_source_tree(tmp_path)

    assert _symbols(result) == ["import urllib.request", "urllib.request.urlopen"]


def test_subprocess_literal_network_tool_is_advisory_but_local_command_is_clean(tmp_path):
    (tmp_path / "shell.py").write_text(
        "import subprocess\n\nsubprocess.run(['curl', url])\nsubprocess.run(['ls'])\n",
        encoding="utf-8",
    )

    result = scan_source_tree(tmp_path)

    assert result["findings"] == [
        {
            "file": "shell.py",
            "line": 3,
            "symbol": "subprocess:curl",
            "reason": "shell-out to network tool",
            "severity": "advisory",
        }
    ]


def test_syntax_error_is_skipped_and_successful_files_are_counted(tmp_path):
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "clean.py").write_text("value = 1\n", encoding="utf-8")

    result = scan_source_tree(tmp_path)

    assert result == {"findings": [], "files_scanned": 1}


def test_vendor_dirs_are_skipped_and_clean_file_has_no_findings(tmp_path):
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("import socket\n", encoding="utf-8")
    (tmp_path / "pure.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    result = scan_source_tree(tmp_path)

    assert result == {"findings": [], "files_scanned": 1}


def test_findings_are_sorted_deterministically(tmp_path):
    (tmp_path / "b.py").write_text("import socket\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("import requests\nimport ssl\n", encoding="utf-8")

    result = scan_source_tree(tmp_path)

    assert [(item["file"], item["line"], item["symbol"]) for item in result["findings"]] == [
        ("a.py", 1, "import requests"),
        ("a.py", 2, "import ssl"),
        ("b.py", 1, "import socket"),
    ]
    assert _symbols(result) == ["import requests", "import ssl", "import socket"]
