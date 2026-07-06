"""Injected in-core credential hardening for the CLI-runner lane (epic #1043 / #1052).

``default_cli_runner`` (``client.py``) spawns a headless coder subprocess for the provider-dispatch lane
— ``web_search``, ``parallel_reason`` today, and the future ``read_offload``. That coder reads UNTRUSTED
content (web results, and — with ``read_offload`` — local files), so a prompt-injection could turn an
inherited secret into an exfiltration channel: the child must never see the orchestrator's tokens/keys,
and the ambient push credential must be unreachable on the default git/gh resolution path.

``core/`` cannot import the private ``scripts/devprocess.credentials`` (the boundary guard is
fail-closed), so this re-implements the same invariants as an in-core, stdlib-only equivalent:

- :func:`scrub_agent_env` strips every secret NAME from the child env (an explicit high-value allow-list
  PLUS a heuristic for anything else).
- :func:`harden_agent_env` additionally redirects the git/gh credential-*discovery* paths into a scratch
  dir, so an agent that naively runs ``git push`` / ``gh`` / a push script cannot authenticate via the
  ambient credential (which lives in the SYSTEM git config / OS keyring, not an env var).

Hard-won invariants (do NOT regress):

- It deliberately does **NOT** touch ``HOME`` / ``USERPROFILE`` — the coder's OWN model credential
  (``~/.claude``, OAuth) is HOME-based and required for it to run (neither name is secret-shaped, so the
  scrub preserves them, and the redirect never sets them).
- It must **NOT** set ``CLAUDE_CONFIG_DIR`` (epic #994 P0 / #996): pinning it was proven on the Spark to
  BREAK the coder's auth (the config lives at ``~/.claude.json``, not ``~/.claude/.claude.json``), and the
  XDG redirect this applies is orthogonal to claude-code's HOME-based auth.

**Containment ceiling (honest scope).** Env-based hardening cannot contain a *deliberately adversarial*
same-user agent: it controls its own child-process env and can re-add a credential helper AND re-select
the OS credential store to read the user-keyed vault. This is the env layer + secret-name scrub
(defence-in-depth), not absolute containment — that needs process/identity isolation (the server-mode
external-agent lane / a container). It is the hard precondition for ``read_offload`` (#1053), not a claim
of absolute containment.

Pure/deterministic, stdlib only; only ever writes under the given scratch dir.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

#: High-value secret NAMES the coder subprocess must never inherit (explicit — plus the heuristic below
#: for anything else). Personal-access-token names (``GH_PAT`` / ``GITHUB_PAT``) carry no TOKEN/KEY/SECRET
#: substring, so ONLY this allow-list catches them; the rest also match the regex (belt-and-suspenders, so
#: a regex edit can never silently un-scrub them). (gitleaks:allow — these are env var NAMES, not values.)
SECRET_ENV_VARS = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "GITHUB_PAT",
    "PYPI_API_TOKEN", "TWINE_PASSWORD",
    "GX10_SERVER_TOKEN",   # the server trust-profile / auth token — never hand it to a spawned coder
})

#: The heuristic for anything not named explicitly (API keys, passwords, generic tokens/credentials).
_SENSITIVE_RE = re.compile(r"TOKEN|SECRET|PASSWORD|APIKEY|_KEY$|_KEY_|CREDENTIAL", re.IGNORECASE)

#: The env keys :func:`harden_agent_env` deliberately SETS (so a leak audit can ignore them): e.g.
#: ``GCM_CREDENTIAL_STORE`` matches the sensitive-name heuristic (contains "CREDENTIAL") yet is a store
#: *selector* ("plaintext"), not a secret value.
HARDENING_KEYS = frozenset({
    "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT",
    "GH_CONFIG_DIR", "GCM_CREDENTIAL_STORE", "GCM_PLAINTEXT_STORE_PATH", "GCM_INTERACTIVE",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
})


def _is_sensitive(name: str) -> bool:
    return name.upper() in SECRET_ENV_VARS or bool(_SENSITIVE_RE.search(name))


def scrub_agent_env(env: Mapping[str, str]) -> dict:
    """Return *env* with every secret NAME removed — what the coder subprocess should inherit."""
    return {k: v for k, v in env.items() if not _is_sensitive(k)}


def leaked_secrets(env: Mapping[str, str], *, ignore: Iterable[str] = ()) -> list:
    """The names of any sensitive vars still present (for a fail-closed assertion). ``[]`` = clean.
    *ignore* exempts deliberately-set config selectors (e.g. :data:`HARDENING_KEYS`) that match the
    sensitive-name heuristic but carry no secret value — it never masks a genuine surviving token."""
    skip = set(ignore)
    return sorted(k for k in env if k not in skip and _is_sensitive(k))


def harden_agent_env(env: Mapping[str, str], scratch_dir: "str | Path") -> dict:
    """:func:`scrub_agent_env` PLUS a redirect of the git/gh *credential-discovery* paths into
    *scratch_dir*, so the ambient push credential is unreachable on the DEFAULT resolution path (an agent
    that naively runs ``git push`` / ``gh`` / a push script cannot authenticate). Preserves
    ``HOME`` / ``USERPROFILE`` (the coder's ``~/.claude`` OAuth) and never sets ``CLAUDE_CONFIG_DIR``
    (#994/#996). Only ever writes under *scratch_dir*; otherwise pure."""
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    gitconfig = scratch / "gitconfig"
    gitconfig_system = scratch / "gitconfig-system"
    # An empty ``helper =`` RESETS git's credential-helper list -> no helper is invoked on the default path
    # (defends even if a global helper is later configured); GIT_CONFIG_NOSYSTEM drops the SYSTEM helper.
    gitconfig.write_text("[credential]\n\thelper =\n", encoding="utf-8")
    gitconfig_system.write_text("", encoding="utf-8")
    gh_dir = scratch / "gh"
    gcm_store = scratch / "gcm-store"   # empty plaintext GCM store: if a helper IS invoked, it reads nothing
    xdg_config = scratch / "xdg-config"
    xdg_data = scratch / "xdg-data"
    xdg_cache = scratch / "xdg-cache"
    for d in (gh_dir, gcm_store, xdg_config, xdg_data, xdg_cache):
        d.mkdir(exist_ok=True)

    hardened = scrub_agent_env(env)
    hardened.update({
        "GIT_CONFIG_NOSYSTEM": "1",                # drop the SYSTEM gitconfig (its credential.helper)
        "GIT_CONFIG_GLOBAL": str(gitconfig),       # override ~/.gitconfig with an empty, helper-reset config
        "GIT_CONFIG_SYSTEM": str(gitconfig_system),
        "GIT_TERMINAL_PROMPT": "0",                # no interactive credential fallback -> fail fast, not hang
        "GH_CONFIG_DIR": str(gh_dir),              # gh honours this over the keyring -> no stored token
        "GCM_CREDENTIAL_STORE": "plaintext",       # cross-platform empty file store (NOT `cache`, which
        "GCM_PLAINTEXT_STORE_PATH": str(gcm_store),#   errors on Windows): a helper, if invoked, reads nothing
        "GCM_INTERACTIVE": "never",                # never pop an interactive auth prompt
        "XDG_CONFIG_HOME": str(xdg_config),        # Linux host: git-credential-cache / gh config
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_CACHE_HOME": str(xdg_cache),
    })
    return hardened


def agent_env_scratch() -> Path:
    """The default scratch dir for the hardened env's empty git/gh config — under the installation state
    root (never the project tree, never a hardcoded path). Fail-soft to the OS temp dir."""
    try:
        from project_registry import ironclad_home
        return Path(ironclad_home()) / "agent-env"
    except Exception:  # noqa: BLE001 — the scratch location is best effort; the scrub is what matters
        return Path(tempfile.gettempdir()) / "ironclad-agent-env"
