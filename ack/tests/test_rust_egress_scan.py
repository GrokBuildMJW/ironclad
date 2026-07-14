from __future__ import annotations

from ack.egress.rust_scan import scan_rust_source_tree


def _symbols(result):
    return [finding["symbol"] for finding in result["findings"]]


def test_std_net_import_and_connect_call_are_advisory_with_lines(tmp_path):
    (tmp_path / "main.rs").write_text(
        "use std::net::TcpStream;\n\nfn main() {\n    let _s = TcpStream::connect(addr);\n}\n",
        encoding="utf-8",
    )

    result = scan_rust_source_tree(tmp_path)

    assert result["files_scanned"] == 1
    assert result["findings"] == [
        {
            "file": "main.rs",
            "line": 1,
            "symbol": "use std::net::TcpStream",
            "reason": "std::net import",
            "severity": "advisory",
            "ecosystem": "rust",
        },
        {
            "file": "main.rs",
            "line": 4,
            "symbol": "TcpStream::connect",
            "reason": "std::net stream connect call",
            "severity": "advisory",
            "ecosystem": "rust",
        },
    ]


def test_known_egress_crate_use_and_identifier_fold_are_advisory(tmp_path):
    (tmp_path / "lib.rs").write_text(
        "use reqwest;\nuse hyper_tls;\n\nfn main() {\n    let _ = reqwest::get(url);\n}\n",
        encoding="utf-8",
    )

    result = scan_rust_source_tree(tmp_path)

    assert result["findings"] == [
        {
            "file": "lib.rs",
            "line": 1,
            "symbol": "use reqwest",
            "reason": "known egress-capable crate use",
            "severity": "advisory",
            "ecosystem": "rust",
        },
        {
            "file": "lib.rs",
            "line": 2,
            "symbol": "use hyper_tls",
            "reason": "known egress-capable crate use",
            "severity": "advisory",
            "ecosystem": "rust",
        },
        {
            "file": "lib.rs",
            "line": 5,
            "symbol": "reqwest::",
            "reason": "known egress-capable crate use",
            "severity": "advisory",
            "ecosystem": "rust",
        },
    ]


def test_command_new_literal_network_tool_is_advisory_but_local_command_is_clean(tmp_path):
    (tmp_path / "shell.rs").write_text(
        'use std::process::Command;\n\n'
        'Command::new("curl").arg(url);\n'
        'Command::new("ls").arg("-la");\n',
        encoding="utf-8",
    )

    result = scan_rust_source_tree(tmp_path)

    assert result["findings"] == [
        {
            "file": "shell.rs",
            "line": 3,
            "symbol": "curl",
            "reason": "shell-out to network tool",
            "severity": "advisory",
            "ecosystem": "rust",
        }
    ]


def test_zero_block_invariant_across_mixed_inputs(tmp_path):
    (tmp_path / "net.rs").write_text(
        'use std::net::{TcpListener, UdpSocket};\nuse aws_sdk_s3;\nstd::process::Command::new("ssh");\n',
        encoding="utf-8",
    )

    result = scan_rust_source_tree(tmp_path)

    assert result["findings"]
    assert all(finding["severity"] == "advisory" for finding in result["findings"])
    assert "block" not in {finding["severity"] for finding in result["findings"]}


def test_unreadable_or_undecodable_file_is_skipped_and_target_is_skipped(tmp_path):
    (tmp_path / "bad.rs").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "ignored.rs").write_text("use std::net::TcpStream;\n", encoding="utf-8")
    (tmp_path / "clean.rs").write_text("this is not valid rust syntax\n", encoding="utf-8")

    result = scan_rust_source_tree(tmp_path)

    assert result == {"findings": [], "files_scanned": 1}


def test_findings_are_sorted_deterministically(tmp_path):
    (tmp_path / "b.rs").write_text('Command::new("curl");\n', encoding="utf-8")
    (tmp_path / "a.rs").write_text("use reqwest;\nuse std::net::UdpSocket;\n", encoding="utf-8")

    result = scan_rust_source_tree(tmp_path)

    assert [(item["file"], item["line"], item["symbol"]) for item in result["findings"]] == [
        ("a.rs", 1, "use reqwest"),
        ("a.rs", 2, "use std::net::UdpSocket"),
        ("b.rs", 1, "curl"),
    ]
    assert _symbols(result) == ["use reqwest", "use std::net::UdpSocket", "curl"]
