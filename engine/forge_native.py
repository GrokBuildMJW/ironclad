"""Native GitHub HTTP forge adapter (#1213, epic #1212) — the gh-independent forge path.

This module is the single home for every GitHub-specific literal (the API host + the request headers).
The rest of the engine selects it only by the vendor-neutral ``forge.adapter`` value and talks to it
through the :class:`~forge_adapters.ForgeAdapter` seam, so the forge tools work with **no `gh` CLI on the
box** (the Spark ``server`` topology) — the whole point of #1212.

Mirrors the native web-search adapter (``websearch_brave.py``): stdlib-only (``urllib.request`` — no
httpx/requests, the wheel stays pydantic-only), stateless, timeout-bounded, host-guarded to the GitHub API
(SSRF), and fail-soft — a network/HTTP/parse failure returns a structured ``("error", msg)`` outcome, never
an exception into the tool loop. The token is passed in already resolved (name-indirected from the
environment by the builder); no secret literal ever lives here.

Outcome convention shared with the seam: every op returns ``(status, payload)`` where status is
``"ok"`` | ``"not_found"`` | ``"error"``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

# The ONLY GitHub literals in the engine (vendor confinement — everything else selects by adapter name).
_API = "https://api.github.com"
_API_VERSION = "2022-11-28"
_ALLOWED_HOSTS = frozenset({"api.github.com"})   # SSRF guard: the native adapter only ever talks to this host

Outcome = Tuple[str, Any]   # ("ok", payload) | ("not_found", None) | ("error", message)


def _checkrun_bucket(run: Any) -> str:
    """Map a REST check-run (status/conclusion) onto the same bucket vocabulary `gh pr checks` uses
    (pass/fail/pending/cancel/skipping), so the cli and native check lists render identically."""
    r = run if isinstance(run, dict) else {}
    if r.get("status") != "completed":
        return "pending"
    concl = r.get("conclusion")
    if concl == "success":
        return "pass"
    if concl in ("failure", "timed_out", "action_required", "startup_failure"):
        return "fail"
    if concl == "cancelled":
        return "cancel"
    return "skipping"   # neutral / skipped / stale / None


def _status_bucket(state: Any) -> str:
    """Map a legacy commit-STATUS state (external CI via the Status API) onto the same bucket vocabulary.
    `gh pr checks` aggregates check-runs AND commit statuses; native must too, or a status-only CI reads as
    'no checks'."""
    s = str(state or "").lower()
    if s == "success":
        return "pass"
    if s in ("failure", "error"):
        return "fail"
    if s == "pending":
        return "pending"
    return "skipping"


def _decide(latest_by_user: dict) -> Optional[str]:
    """Reduce {user → latest decisive review state} to a reviewDecision: any CHANGES_REQUESTED wins, else
    any APPROVED, else None. (Best-effort — does not model required reviewers.)"""
    states = set(latest_by_user.values())
    if "CHANGES_REQUESTED" in states:
        return "CHANGES_REQUESTED"
    if "APPROVED" in states:
        return "APPROVED"
    return None


class NativeForgeAdapter:
    """Calls the GitHub REST API directly over stdlib HTTP. Requires a token AND an explicit ``owner/repo``
    (there is no ambient git remote to infer one from, unlike `gh`)."""

    name = "native"   # the engine never sees a vendor name; the literal stays inside this module

    def __init__(self, token: str, repo: str, *, api: str = _API, timeout_s: float = 30.0,
                 opener: Optional[Callable[..., Any]] = None) -> None:
        self._token = token or ""
        self._repo = (repo or "").strip().strip("/")
        self._api = api.rstrip("/")
        self._timeout = float(timeout_s)
        # Injectable opener (default = the stdlib urlopen) keeps the adapter network-free under test.
        self._open = opener or urllib.request.urlopen

    def available(self) -> bool:
        return bool(self._token) and bool(self._repo)

    # ── HTTP core ────────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Outcome:
        """One GitHub API call. Returns ("ok", parsed) / ("not_found", status) / ("error", msg). Never raises.
        ``path`` is a repo-relative path (e.g. ``/issues/17``) — the owner/repo prefix is added here."""
        url = f"{self._api}/repos/{self._repo}{path}"
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host not in _ALLOWED_HOSTS:   # SSRF guard: never let a mis-set api host reach an arbitrary target
            return ("error", f"refused non-GitHub host {host!r} (SSRF guard)")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": "ironclad-forge",
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return ("ok", json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ("not_found", 404)
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8", "replace")).get("message", "")
            except Exception:  # noqa: BLE001 — a body-less/HTML error still surfaces the code
                pass
            return ("error", f"HTTP {e.code}{(': ' + detail) if detail else ''}")
        except Exception as ex:  # noqa: BLE001 — timeout / network / decode → a readable outcome, never a raise
            return ("error", f"request failed: {ex!r}")

    # ── operations (the seam contract) ───────────────────────────────────────
    def view_issue(self, number: int) -> Outcome:
        st, payload = self._request("GET", f"/issues/{number}")
        if st == "not_found":
            # A 404 on the issue endpoint is AMBIGUOUS: a genuinely missing issue OR a missing/inaccessible
            # repo (GitHub returns 404, not 403, for a private repo a token can't see). Disambiguate with a
            # repo-root probe so a repo problem is a real ERROR, not a false authoritative NOT_FOUND — the
            # #1208 regression guard on the native path (mirrors the cli path's repo-resolution narrowing).
            rst, _ = self._request("GET", "")   # GET /repos/{owner}/{repo}
            if rst != "ok":
                return ("error", f"repository {self._repo!r} not found or inaccessible "
                                 f"(check forge.repo and the token's scope)")
            return ("not_found", None)
        if st != "ok":
            return ("error", str(payload))
        # Normalize the REST shape to the same shape gh --json yields (so the handler renders identically):
        # state OPEN/CLOSED (gh is upper-case), url = html_url, labels [{name}], milestone {title}|None.
        d = payload if isinstance(payload, dict) else {}
        return ("ok", {
            "number":    d.get("number"),
            "state":     str(d.get("state") or "").upper(),
            "title":     d.get("title") or "",
            "labels":    [{"name": (l or {}).get("name", "")} for l in (d.get("labels") or [])],
            "milestone": ({"title": (d.get("milestone") or {}).get("title")} if d.get("milestone") else None),
            "url":       d.get("html_url") or "",
            "body":      d.get("body") or "",
        })

    def list_labels(self) -> Optional[set]:
        """Every repo label (paginated). Returns None on any error (fail-soft — the caller then skips
        label validation rather than blocking a create), matching the gh path's fail-soft contract."""
        names: set = set()
        for page in range(1, 6):   # up to 500 labels (100/page) — matches the gh --limit 300 spirit, bounded
            st, payload = self._request("GET", f"/labels?per_page=100&page={page}")
            if st != "ok":
                return None if not names else names
            batch = payload if isinstance(payload, list) else []
            for lb in batch:
                nm = (lb or {}).get("name")
                if nm:
                    names.add(nm)
            if len(batch) < 100:
                break
        return names

    def _resolve_milestone(self, title: str) -> Outcome:
        """Milestone TITLE → number (REST create takes a number; gh takes a title). Matches gh's behaviour of
        failing when the milestone does not exist."""
        for page in range(1, 6):
            st, payload = self._request("GET", f"/milestones?state=all&per_page=100&page={page}")
            if st != "ok":
                return ("error", f"could not resolve milestone {title!r}: {payload}")
            batch = payload if isinstance(payload, list) else []
            for ms in batch:
                if (ms or {}).get("title") == title:
                    return ("ok", ms.get("number"))
            if len(batch) < 100:
                break
        return ("error", f"milestone {title!r} not found in {self._repo}")

    def create_issue(self, title: str, body_file: Path, labels: List[str],
                     milestone: Optional[str]) -> Outcome:
        """Create an issue. Body comes from a FILE (escape-free, same contract as the gh path). Returns
        ("ok", {"url":…, "number":…}) so the caller can optionally sub-issue-link it."""
        try:
            body = Path(body_file).read_text(encoding="utf-8", errors="replace")
        except Exception as ex:  # noqa: BLE001
            return ("error", f"could not read body_file: {ex!r}")
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if milestone:
            mst, mnum = self._resolve_milestone(milestone)
            if mst != "ok":
                return ("error", str(mnum))
            payload["milestone"] = mnum
        st, res = self._request("POST", "/issues", payload)
        if st != "ok":
            return ("error", f"create failed: {res}")
        d = res if isinstance(res, dict) else {}
        return ("ok", {"url": d.get("html_url") or "", "number": d.get("number")})

    def link_sub_issue(self, parent: str, child: dict) -> Outcome:
        """Link an existing issue as a native sub-issue of *parent* via the REST sub-issues API. Signature
        matches the seam (``child`` is the create-result dict ``{"url":…, "number":…}``). The API keys the
        child by its DATABASE id (not its number), so resolve the child first. Fail-soft — the caller reports
        a link failure alongside the already-created issue, never raises."""
        try:
            pnum = int(str(parent).strip().lstrip("#"))
        except (TypeError, ValueError):
            return ("error", f"parent {parent!r} is not a numeric issue")
        try:
            child_number = int((child or {}).get("number"))
        except (TypeError, ValueError):
            return ("error", "child issue has no numeric number to link")
        cst, cpayload = self._request("GET", f"/issues/{child_number}")
        if cst != "ok":
            return ("error", f"could not resolve child #{child_number}: {cpayload}")
        child_id = (cpayload if isinstance(cpayload, dict) else {}).get("id")
        if not child_id:
            return ("error", f"child #{child_number} has no id")
        st, res = self._request("POST", f"/issues/{pnum}/sub_issues", {"sub_issue_id": child_id})
        if st != "ok":
            return ("error", str(res))
        return ("ok", None)

    def create_pr(self, title: str, body_file: Path, base: Optional[str], head: Optional[str],
                  draft: bool) -> Outcome:
        """Open a PR via POST /pulls. REST requires an explicit `head` (source branch) — there is no local
        git to infer it, unlike `gh` — and a `base`; an absent base defaults to the repo's default branch.
        Body comes from a FILE (escape-free). Returns ("ok", pr_url)."""
        if not head:
            return ("error", "native create_pr needs `head` (the source branch) — there is no local git to infer it")
        b = base
        if not b:
            rst, repo = self._request("GET", "")   # GET /repos/{owner}/{repo} → default_branch
            if rst != "ok":
                return ("error", f"could not resolve the default base branch: {repo}")
            b = (repo if isinstance(repo, dict) else {}).get("default_branch") or "main"
        try:
            body = Path(body_file).read_text(encoding="utf-8", errors="replace")
        except Exception as ex:  # noqa: BLE001
            return ("error", f"could not read body_file: {ex!r}")
        st, res = self._request("POST", "/pulls",
                                {"title": title, "head": head, "base": b, "body": body, "draft": bool(draft)})
        if st != "ok":
            return ("error", f"create failed: {res}")
        return ("ok", (res if isinstance(res, dict) else {}).get("html_url") or "")

    def comment_on_issue(self, number: int, body_file: Path) -> Outcome:
        """Append a comment to an issue via POST /issues/{n}/comments. Body from a FILE (escape-free).
        Returns ("ok", comment_url). A 404 is disambiguated (repo-root probe) so a missing/inaccessible repo
        is a real ERROR, not a false NOT_FOUND — same #1208 guard as view_issue."""
        try:
            body = Path(body_file).read_text(encoding="utf-8", errors="replace")
        except Exception as ex:  # noqa: BLE001
            return ("error", f"could not read body_file: {ex!r}")
        st, res = self._request("POST", f"/issues/{number}/comments", {"body": body})
        if st == "not_found":
            rst, _ = self._request("GET", "")   # GET /repos/{owner}/{repo}
            if rst != "ok":
                return ("error", f"repository {self._repo!r} not found or inaccessible "
                                 f"(check forge.repo and the token's scope)")
            return ("not_found", None)
        if st != "ok":
            return ("error", f"comment failed: {res}")
        return ("ok", (res if isinstance(res, dict) else {}).get("html_url") or "")

    def pr_status(self, number: int) -> Outcome:
        """Read a PR's CI/mergeability snapshot: GET /pulls/{n} (state, mergeable, mergeable_state) + GET the
        head commit's check-runs. 404-disambiguated (repo probe) like view_issue. Returns the same structured
        dict the cli path yields, so the handler formats one way. Non-blocking — one snapshot, no waiting."""
        st, pr = self._request("GET", f"/pulls/{number}")
        if st == "not_found":
            rst, _ = self._request("GET", "")
            if rst != "ok":
                return ("error", f"repository {self._repo!r} not found or inaccessible "
                                 f"(check forge.repo and the token's scope)")
            return ("not_found", None)
        if st != "ok":
            return ("error", str(pr))
        pr = pr if isinstance(pr, dict) else {}
        sha = ((pr.get("head") or {}).get("sha")) or ""
        checks: list = []
        if sha:
            # (a) check-runs (GitHub Actions & apps), PAGINATED — a failing run on page 2 must not be missed.
            for page in range(1, 6):
                cst, cr = self._request("GET", f"/commits/{sha}/check-runs?per_page=100&page={page}")
                if cst != "ok":
                    break
                runs = (cr if isinstance(cr, dict) else {}).get("check_runs") or []
                for run in runs:
                    checks.append({"name": (run or {}).get("name", ""), "bucket": _checkrun_bucket(run)})
                if len(runs) < 100:
                    break
            # (b) legacy commit STATUSES (external CI via the Status API) — `gh pr checks` includes these, so
            # native must too or a status-only CI reads as 'no checks / mergeable'.
            sst, sres = self._request("GET", f"/commits/{sha}/status")
            if sst == "ok":
                for stx in ((sres if isinstance(sres, dict) else {}).get("statuses") or []):
                    checks.append({"name": (stx or {}).get("context", ""),
                                   "bucket": _status_bucket((stx or {}).get("state"))})
        mergeable = pr.get("mergeable")   # true / false / null (GitHub computes it asynchronously)
        m = "MERGEABLE" if mergeable is True else ("CONFLICTING" if mergeable is False else "UNKNOWN")
        state = "MERGED" if pr.get("merged") else (str(pr.get("state") or "").upper() or None)
        return ("ok", {"checks": checks, "mergeable": m,
                       "mergeStateStatus": (str(pr.get("mergeable_state") or "").upper() or None),
                       "reviewDecision": self._review_decision(number),   # best-effort from /reviews (no GraphQL)
                       "state": state})

    def _review_decision(self, number: int) -> Optional[str]:
        """Approximate the PR reviewDecision from REST /reviews (GitHub only exposes the real field via
        GraphQL). Take each reviewer's LATEST decisive review (APPROVED / CHANGES_REQUESTED, ignoring
        COMMENTED / DISMISSED / PENDING): any outstanding CHANGES_REQUESTED ⇒ CHANGES_REQUESTED, else any
        APPROVED ⇒ APPROVED, else None. Best-effort (does not model required-reviewer / CODEOWNERS rules);
        fail-soft — a hiccup returns None rather than blocking the snapshot."""
        latest: dict = {}
        for page in range(1, 6):
            st, res = self._request("GET", f"/pulls/{number}/reviews?per_page=100&page={page}")
            if st != "ok":
                return None if not latest else _decide(latest)
            batch = res if isinstance(res, list) else []
            for rv in batch:
                state = str((rv or {}).get("state") or "").upper()
                if state not in ("APPROVED", "CHANGES_REQUESTED"):
                    continue   # COMMENTED / DISMISSED / PENDING do not set a decision
                user = (((rv or {}).get("user") or {}).get("login")) or ""
                latest[user] = state   # /reviews is chronological → the last one per user wins
            if len(batch) < 100:
                break
        return _decide(latest)
