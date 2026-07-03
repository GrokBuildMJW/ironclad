"""#1071 (epic #1065): per-principal identity + RBAC + multi-tenant memory namespacing. The pure foundation
for multi-user/enterprise — a Principal (id/role/tenant), a deny-by-default role→danger-tier policy, token→
principal resolution, and tenant-scoped memory. Default-off (single-tenant byte-identical); the full
request-path enforcement is remaining scope (ADR-0014)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_CORE = Path(__file__).resolve().parents[2]
for p in (str(_CORE), str(_CORE / "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ack import authz  # noqa: E402


def test_rbac_role_tier_matrix():
    assert authz.authorize("admin", authz.DESTRUCTIVE) is True
    assert authz.authorize("operator", authz.DESTRUCTIVE) is False       # operator: all but destructive
    assert authz.authorize("operator", authz.MUTATING) is True
    assert authz.authorize("agent", authz.COSTLY) is True
    assert authz.authorize("agent", authz.DESTRUCTIVE) is False          # the autonomous agent: no destructive
    assert authz.authorize("reader", authz.READ_ONLY) is True
    assert authz.authorize("reader", authz.MUTATING) is False


def test_rbac_deny_by_default():
    assert authz.authorize("nonexistent-role", authz.READ_ONLY) is False
    assert authz.authorize("admin", "bogus-tier") is False


def test_resolve_principal_from_token_map():
    principals = {"tok-a": {"id": "alice", "role": "admin", "tenant": "acme"}}
    p = authz.resolve_principal("tok-a", principals)
    assert p.id == "alice" and p.role == "admin" and p.tenant == "acme"
    assert authz.resolve_principal("unknown", principals) is authz.ANONYMOUS   # unknown → anonymous
    assert authz.resolve_principal("x", None) is authz.ANONYMOUS               # no map → anonymous


def test_tenant_scope_isolates_but_default_is_noop():
    assert authz.tenant_scope("proj::main", "default") == "proj::main"    # default → no-op (byte-identical)
    assert authz.tenant_scope("proj::main", "") == "proj::main"
    assert authz.tenant_scope("proj::main", "acme") == "acme::proj::main"  # tenant-prefixed isolation


def test_engine_authorize_action_gate(monkeypatch):
    import gx10
    monkeypatch.setattr(gx10, "MULTI_TENANT", False)
    assert gx10._authorize_action("reader", authz.DESTRUCTIVE) is True   # off → allow-all (single-tenant)
    monkeypatch.setattr(gx10, "MULTI_TENANT", True)
    assert gx10._authorize_action("reader", authz.DESTRUCTIVE) is False  # on → RBAC deny
    assert gx10._authorize_action("admin", authz.DESTRUCTIVE) is True


def test_engine_tenant_mem_scope_gate(monkeypatch):
    import gx10
    monkeypatch.setattr(gx10, "MULTI_TENANT", False)
    assert gx10._tenant_mem_scope("s", "acme") == "s"                     # off → byte-identical
    monkeypatch.setattr(gx10, "MULTI_TENANT", True)
    assert gx10._tenant_mem_scope("s", "acme") == "acme::s"


def test_multi_tenant_defaults_off():
    import gx10
    assert gx10.MULTI_TENANT is False
