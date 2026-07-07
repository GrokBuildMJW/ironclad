"""Forge adapter seam (#1213, epic #1212) — a vendor-neutral selection layer for the tracker/forge I/O.

Mirrors the web-search adapter seam (``websearch_adapters.py``): the forge tools (`create_issue`,
`view_issue`, and the follow-ups `create_pr`/`comment_on_issue`/… under #1212) talk to the forge ONLY
through this seam, so the SAME tools work whether the box has the `gh` CLI (``cli`` adapter, today's
behaviour, byte-identical) or not (``native`` adapter, a stdlib-`urllib` GitHub client in
``forge_native.py`` — the Spark ``server`` topology). Selection is by the vendor-neutral ``forge.adapter``
config value; no gh/GitHub literal leaks into the gate, prompt, tool schema, or handlers.

Every operation returns a uniform ``(status, payload)`` outcome — ``"ok"`` | ``"not_found"`` | ``"error"``
— so the gx10 handlers format one way regardless of which adapter ran. stdlib-only, fail-soft, never raises
into the tool loop.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

Outcome = Tuple[str, Any]   # ("ok", payload) | ("not_found", None) | ("error", message)

# The gh --json field set view_issue reads (kept identical to the pre-seam handler so rendering is unchanged).
_ISSUE_FIELDS = "number,state,title,labels,milestone,url,body"


def _classify_missing(err: str) -> bool:
    """gh exits non-zero when an issue does not exist — an AUTHORITATIVE 'not found', not an inference.
    Narrowed so a REPOSITORY-resolution failure (a mis-set forge.repo also says 'could not resolve') is a
    real error, not a false not-found. (Kept identical to the #1208 view_issue detector.)"""
    low = (err or "").lower()
    return ("could not resolve to an issue" in low
            or "could not resolve to a pull request" in low
            or ("not found" in low and "repositor" not in low))


class ForgeAdapter:
    """Seam interface. ``available()`` is the transport-level capability check; the ops execute one
    read/write forge call and never raise."""

    name = "base"

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def view_issue(self, number: int) -> Outcome:  # pragma: no cover - overridden
        raise NotImplementedError

    def list_labels(self) -> Optional[set]:  # pragma: no cover - overridden
        return None

    def create_issue(self, title: str, body_file: Path, labels: List[str],
                     milestone: Optional[str]) -> Outcome:  # pragma: no cover - overridden
        raise NotImplementedError

    def link_sub_issue(self, parent: str, child: dict) -> Outcome:  # pragma: no cover - overridden
        raise NotImplementedError

    def create_pr(self, title: str, body_file: Path, base: Optional[str], head: Optional[str],
                  draft: bool) -> Outcome:  # pragma: no cover - overridden
        raise NotImplementedError

    def comment_on_issue(self, number: int, body_file: Path) -> Outcome:  # pragma: no cover - overridden
        raise NotImplementedError

    def pr_status(self, number: int) -> Outcome:  # pragma: no cover - overridden
        raise NotImplementedError


class CliForgeAdapter(ForgeAdapter):
    """Today's behaviour: the ambient `gh` CLI via subprocess. Builds the exact same argv as the pre-seam
    handlers, so the forge tools are byte-identical on a box that has `gh`."""

    name = "cli"

    def __init__(self, repo: str = "") -> None:
        self._repo = (repo or "").strip()

    def _repo_args(self) -> List[str]:
        return ["--repo", self._repo] if self._repo else []

    def available(self) -> bool:
        # shutil.which (not a cached bool) so the boot-time capability probe stays live + monkeypatchable.
        return shutil.which("gh") is not None

    def view_issue(self, number: int) -> Outcome:
        cmd = ["gh", "issue", "view", str(number), "--json", _ISSUE_FIELDS] + self._repo_args()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
        except Exception as ex:  # noqa: BLE001 — a forge/network hiccup is a tool error, not a crash
            return ("error", f"could not run gh: {ex!r}")
        if r.returncode != 0:
            err = ((r.stderr or r.stdout) or "").strip()
            if _classify_missing(err):
                return ("not_found", None)
            return ("error", f"gh issue view failed: {err[:400]}")
        try:
            return ("ok", json.loads(r.stdout or "{}"))
        except Exception:  # noqa: BLE001 — a malformed gh payload is a tool error, not a crash
            return ("error", f"unparseable gh output: {(r.stdout or '')[:200]}")

    def list_labels(self) -> Optional[set]:
        cmd = ["gh", "label", "list", "--limit", "300", "--json", "name", "-q", ".[].name"] + self._repo_args()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
        except Exception:  # noqa: BLE001
            return None
        if r.returncode != 0:
            return None
        return {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}

    def create_issue(self, title: str, body_file: Path, labels: List[str],
                     milestone: Optional[str]) -> Outcome:
        cmd = ["gh", "issue", "create", "--title", str(title), "--body-file", str(body_file)] + self._repo_args()
        for lb in labels:
            cmd += ["--label", lb]
        if milestone:
            cmd += ["--milestone", str(milestone)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
        except Exception as ex:  # noqa: BLE001
            return ("error", f"could not run gh: {ex!r}")
        if r.returncode != 0:
            return ("error", f"gh issue create failed: {((r.stderr or r.stdout) or '').strip()[:400]}")
        url = (r.stdout or "").strip()
        num = None
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            num = int(tail)
        return ("ok", {"url": url, "number": num})

    def link_sub_issue(self, parent: str, child: dict) -> Outcome:
        child_ref = (child or {}).get("url") or str((child or {}).get("number") or "")
        cmd = ["gh", "issue", "edit", child_ref, "--parent", parent] + self._repo_args()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
        except Exception as ex:  # noqa: BLE001
            return ("error", f"{ex!r}")
        if r.returncode != 0:
            return ("error", ((r.stderr or r.stdout) or "").strip()[:200])
        return ("ok", None)

    def create_pr(self, title: str, body_file: Path, base: Optional[str], head: Optional[str],
                  draft: bool) -> Outcome:
        cmd = ["gh", "pr", "create", "--title", str(title), "--body-file", str(body_file)] + self._repo_args()
        if base:
            cmd += ["--base", str(base)]
        if head:
            cmd += ["--head", str(head)]
        if draft:
            cmd += ["--draft"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
        except Exception as ex:  # noqa: BLE001
            return ("error", f"could not run gh: {ex!r}")
        if r.returncode != 0:
            # verbatim-surfaces gh's "must first push the current branch" so the model pushes, not guesses
            return ("error", f"gh pr create failed: {((r.stderr or r.stdout) or '').strip()[:400]}")
        return ("ok", (r.stdout or "").strip())

    def comment_on_issue(self, number: int, body_file: Path) -> Outcome:
        cmd = ["gh", "issue", "comment", str(number), "--body-file", str(body_file)] + self._repo_args()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
        except Exception as ex:  # noqa: BLE001
            return ("error", f"could not run gh: {ex!r}")
        if r.returncode != 0:
            err = ((r.stderr or r.stdout) or "").strip()
            if _classify_missing(err):
                return ("not_found", None)
            return ("error", f"gh issue comment failed: {err[:400]}")
        return ("ok", (r.stdout or "").strip())

    def pr_status(self, number: int) -> Outcome:
        # existence + mergeability via `gh pr view` (a clean not_found for a missing PR).
        vcmd = ["gh", "pr", "view", str(number), "--json",
                "state,mergeable,mergeStateStatus,reviewDecision"] + self._repo_args()
        try:
            vr = subprocess.run(vcmd, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=60)
        except Exception as ex:  # noqa: BLE001
            return ("error", f"could not run gh: {ex!r}")
        if vr.returncode != 0:
            err = ((vr.stderr or vr.stdout) or "").strip()
            low = err.lower()
            if _classify_missing(err) or "could not resolve to a pullrequest" in low:
                return ("not_found", None)
            return ("error", f"gh pr view failed: {err[:400]}")
        try:
            merge = json.loads(vr.stdout or "{}")
        except Exception:  # noqa: BLE001
            merge = {}
        # checks via `gh pr checks`. GOTCHA: it EXITS NON-ZERO as DATA (pending=8, fail=1, pass=0) — so parse
        # stdout FIRST; a non-zero exit with a JSON payload is a real result, not an error. Empty stdout ("no
        # checks reported") ⇒ no checks yet. Fail-soft: mergeability is already captured above.
        ccmd = ["gh", "pr", "checks", str(number), "--json", "name,state,bucket,link"] + self._repo_args()
        checks: List[dict] = []
        try:
            cr = subprocess.run(ccmd, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=60)
            cout = (cr.stdout or "").strip()
            if cout:
                parsed = json.loads(cout)
                checks = parsed if isinstance(parsed, list) else []
        except Exception:  # noqa: BLE001
            checks = []
        return ("ok", {"checks": checks, "mergeable": merge.get("mergeable"),
                       "mergeStateStatus": merge.get("mergeStateStatus"),
                       "reviewDecision": merge.get("reviewDecision"), "state": merge.get("state")})


class MockForgeAdapter(ForgeAdapter):
    """Deterministic, network-free adapter for tests / a zero-config demo. Always available."""

    name = "mock"

    def __init__(self, issues: Optional[dict] = None) -> None:
        # issues: {number: {state,title,labels,milestone,url,body}}
        self._issues = issues or {}

    def available(self) -> bool:
        return True

    def view_issue(self, number: int) -> Outcome:
        d = self._issues.get(int(number))
        return ("ok", d) if d else ("not_found", None)

    def list_labels(self) -> Optional[set]:
        return {"type/bug", "type/chore", "area/engine"}

    def create_issue(self, title: str, body_file: Path, labels: List[str],
                     milestone: Optional[str]) -> Outcome:
        return ("ok", {"url": "https://github.com/mock/mock/issues/1", "number": 1})

    def link_sub_issue(self, parent: str, child: dict) -> Outcome:
        return ("ok", None)

    def create_pr(self, title: str, body_file: Path, base: Optional[str], head: Optional[str],
                  draft: bool) -> Outcome:
        return ("ok", "https://github.com/mock/mock/pull/1")

    def comment_on_issue(self, number: int, body_file: Path) -> Outcome:
        return ("ok", f"https://github.com/mock/mock/issues/{number}#issuecomment-1")

    def pr_status(self, number: int) -> Outcome:
        return ("ok", {"checks": [{"name": "ci", "bucket": "pass"}], "mergeable": "MERGEABLE",
                       "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED", "state": "OPEN"})


class UnavailableForgeAdapter(ForgeAdapter):
    """A configured-but-not-usable adapter: ``available()`` is False and ops return a clean reason
    (e.g. ``forge.adapter=native`` with no token/repo, or ``forge.enabled=false``)."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    def available(self) -> bool:
        return False

    def _err(self, *_a, **_k) -> Outcome:
        return ("error", self._reason)

    view_issue = _err          # type: ignore[assignment]
    create_issue = _err        # type: ignore[assignment]
    link_sub_issue = _err      # type: ignore[assignment]
    create_pr = _err           # type: ignore[assignment]
    comment_on_issue = _err    # type: ignore[assignment]
    pr_status = _err           # type: ignore[assignment]


def build_forge_adapter(*, adapter: str, repo: str, token: str) -> ForgeAdapter:
    """Select the forge adapter from the vendor-neutral ``forge.adapter`` value. Never raises.

    * ``mock``   → :class:`MockForgeAdapter`.
    * ``native`` → the stdlib-`urllib` GitHub client (``forge_native``), keyed by a token + an explicit
      ``owner/repo`` (there is no ambient git remote to infer one). Missing token/repo ⇒ Unavailable.
    * anything else / ``cli`` / unset → :class:`CliForgeAdapter` (today's `gh` behaviour, the default).
    """
    a = (adapter or "cli").strip().lower()
    if a == "mock":
        return MockForgeAdapter()
    if a == "native":
        if not token:
            return UnavailableForgeAdapter("native", "the forge token is not set in the environment")
        if not repo:
            return UnavailableForgeAdapter("native", "the native forge adapter needs forge.repo (owner/repo)")
        from forge_native import NativeForgeAdapter
        return NativeForgeAdapter(token, repo)
    return CliForgeAdapter(repo)
