"""Versioned known-egress-capable Cargo crates.

This data set is a tripwire input, not a safety proof. A crate absent from
this set may still be egress-capable; absence is not proof that a dependency
closure is clean.

Contract changes must update ``KNOWN_EGRESS_CRATES_VERSION`` and include tests
or evidence for additions/removals. Names are stored in Cargo canonical form:
lowercase with each run of ``-`` or ``_`` collapsed to ``-``. Wildcards and
prefix matches are deliberately excluded; every crate name is concrete.
"""
from __future__ import annotations

KNOWN_EGRESS_CRATES_VERSION = "2026.07.1"

KNOWN_EGRESS_CRATES = frozenset(
    {
        "actix-web",
        "attohttpc",
        "awc",
        "aws-config",
        "aws-sdk-dynamodb",
        "aws-sdk-s3",
        "azure-core",
        "curl",
        "google-cloud-storage",
        "hickory-resolver",
        "hyper",
        "hyper-tls",
        "hyper-util",
        "isahc",
        "lapin",
        "lettre",
        "minreq",
        "mongodb",
        "mysql-async",
        "paho-mqtt",
        "postgres",
        "quinn",
        "rdkafka",
        "redis",
        "reqwest",
        "rumqttc",
        "rusoto-core",
        "rusoto-s3",
        "russh",
        "ssh2",
        "surf",
        "tokio-postgres",
        "tokio-tungstenite",
        "tonic",
        "trust-dns-resolver",
        "tungstenite",
        "ureq",
        "websocket",
    }
)

__all__ = ["KNOWN_EGRESS_CRATES", "KNOWN_EGRESS_CRATES_VERSION"]
