"""Registration quality gate (ADR-0001 D2, #34) — no unchecked skill enters the toolset.

Before a generated/installed skill is registered it must pass this gate:

- **tool** — a doctor preflight: the module parses + loads, exposes a ``CASE`` with a
  non-empty ``capability``, a **synchronous** ``run`` whose signature yields a valid tool
  schema; and an auto-generated **test file** ships alongside it.
- **playbook** — its ``SKILL.md`` frontmatter validates against the schema, its references are
  readable, and its ``scripts/check`` (the file-first gate) exits 0 if present.

The heavier behavioral ``eval/`` (A/B + judge) stays **opt-in** (not part of this gate).
Pure/deterministic except the optional ``scripts/check`` subprocess.
"""
from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GateResult:
    passed: bool
    kind: str
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


#: The marker the paved-road generator emits into a freshly-scaffolded skill stub (S11 / #630). It is the
#: machine token the generation-completeness gate keys on: an item that still carries it is an UNFILLED
#: scaffold and is refused — so a stub can never be discovered/registered as a real capability. An author
#: removes the line when they implement ``run()``.
SCAFFOLD_SENTINEL = "ACK-SCAFFOLD-SENTINEL"


def has_scaffold_sentinel(py_path: str | Path) -> bool:
    """True iff *py_path* still carries the unfilled-scaffold sentinel (the item was generated but never
    implemented). Pure + fail-soft: an unreadable file is treated as NOT a scaffold (the schema/preflight
    gate already rejects an unreadable/broken file, so this never masks that)."""
    try:
        return SCAFFOLD_SENTINEL in Path(py_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


#: env var NAMES (not values — gitleaks:allow) a hermetic run must NOT inherit: credentials/secrets +
#: net/memory endpoints, so the sibling test cannot reach GitHub / PyPI / the memory service / a paid API.
#: Self-contained (ack/ must not import the private dev-loop substrate); mirrors its scrub set.
_HERMETIC_SECRET_NAMES = frozenset({  # gitleaks:allow — env var NAMES, not secret values
    "GH_TOKEN", "GITHUB_TOKEN", "UPSTREAM_TOKEN", "PROJECTS_TOKEN", "DEV_SYNC_TOKEN",  # gitleaks:allow
    "PYPI_API_TOKEN", "TWINE_PASSWORD", "GX10_DEVLOOP_MARKER_KEY", "GX10_DEVLOOP_GO_SECRET",  # gitleaks:allow
    "GX10_MEMORY_URL", "GX10_WARM_URL", "GX10_LIVE_URL", "GX10_LIVE_TOKEN",  # gitleaks:allow
    # auth/egress channels the TOKEN|SECRET|KEY|CREDENTIAL regex does NOT catch by name:
    "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "DOCKER_AUTH_CONFIG", "NETRC",  # gitleaks:allow
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "GIT_CONFIG", "GIT_CONFIG_GLOBAL",  # gitleaks:allow
})
_HERMETIC_SECRET_RE = re.compile(r"TOKEN|SECRET|PASSWORD|APIKEY|_KEY$|_KEY_|CREDENTIAL", re.IGNORECASE)
#: Hard wall-clock cap for a single sibling-test run — a hung/looping generated test never blocks the gate.
HERMETIC_TEST_TIMEOUT = 30


def _hermetic_env() -> dict:
    """A scrubbed copy of the process env for the hermetic test subprocess: every credential/secret +
    net/memory endpoint dropped, ``import ack`` made resolvable (PYTHONPATH = the ack package's parent),
    and credential prompts disabled (so a careless test fails fast instead of hanging)."""
    keep = {k: v for k, v in os.environ.items()
            if not (k.upper() in _HERMETIC_SECRET_NAMES or _HERMETIC_SECRET_RE.search(k))}
    ack_parent = str(Path(__file__).resolve().parent.parent)   # the dir holding the ``ack`` package
    keep["PYTHONPATH"] = ack_parent + ((os.pathsep + keep["PYTHONPATH"]) if keep.get("PYTHONPATH") else "")
    keep["GIT_TERMINAL_PROMPT"] = "0"
    keep["PYTHONDONTWRITEBYTECODE"] = "1"
    keep.setdefault("PYTHONUTF8", "1")
    return keep


def run_sibling_test_hermetic(py_path: str | Path, *, timeout: int = HERMETIC_TEST_TIMEOUT) -> tuple[bool, str]:
    """Execute the generated skill's sibling test in a HERMETIC sandbox (S11b-2 / #630): a fresh subprocess
    running ``pytest`` on ``tests/test_<stem>.py`` with a SCRUBBED env (:func:`_hermetic_env`), a hard
    *timeout*, and a tmp cwd. Returns ``(ok, detail)``. Fail-soft: a missing test, a non-zero exit, a
    timeout, or any spawn error is a FAILURE, never a raise.

    This RUNS generated test code — call it only on items you intend to validate. The containment is
    **best-effort defense-in-depth, NOT an absolute OS sandbox**:
    - the env scrub drops the known credential/secret + net/memory channels so a naive test can't
      authenticate to GitHub / PyPI / the memory service — but it cannot stop a determined test from
      opening a raw socket;
    - the tmp cwd keeps a test's RELATIVE writes out of the repo, but a test can still write via an
      absolute path / ``__file__``;
    - the *timeout* bounds the direct ``pytest`` child (it is killed on expiry); a test that spawns its
      own detached children may leave orphans.
    The child runs with plain asserts (``--assert=plain``) — no pytest assertion rewriting — so the #1665
    Python-3.14 rewrite flake cannot recur in the nested process; a failure still reports the line + ``AssertionError``.
    ``gate_generated`` does NOT run this unless ``execute=True``."""
    p = Path(py_path).resolve()                      # absolute, so the test path resolves under cwd=tmp
    test_file = p.parent.parent / "tests" / f"test_{p.stem}.py"
    if not test_file.is_file():
        return False, f"no sibling test ({test_file.name})"
    try:
        with tempfile.TemporaryDirectory(prefix="ack-hermetic-") as tmp:
            cp = subprocess.run(
                [
                    sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider",
                    # Avoid #1665's nested assertion-rewrite exposure. Plain mode loses pytest's rich
                    # assertion introspection, but still reports the failing line and AssertionError,
                    # which is sufficient for this pass/fail gate and its already-bounded detail tail.
                    "--assert=plain",
                ],
                cwd=tmp, env=_hermetic_env(), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return False, f"sibling test exceeded the {timeout}s hard timeout (hung/looping?)"
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001 — a spawn failure is a gate failure
        return False, f"could not run sibling test: {e!r}"
    if cp.returncode == 0:
        return True, "sibling test passed (hermetic)"
    tail = ((cp.stdout or "") + (cp.stderr or "")).strip()[-400:]
    return False, f"sibling test failed (rc={cp.returncode}): {tail}"


def gate_generated(py_path: str | Path, *, execute: bool = False) -> GateResult:
    """Generation-completeness gate (S11 / #630) — STRICTER than the registration gate ``gate_tool``: a
    generated item must pass the doctor preflight (CASE + schema + a sibling test) **and** be FILLED — it
    may not still carry the scaffold sentinel (an un-implemented stub). With ``execute=True`` it ALSO runs
    the behavioural sibling test hermetically (:func:`run_sibling_test_hermetic`) — opt-in, because it
    executes generated code; the default (``execute=False``) is the pure, no-execution check. Returns a
    combined GateResult (kind ``generated``)."""
    base = gate_tool(py_path)
    reasons = list(base.reasons)
    if has_scaffold_sentinel(py_path):
        reasons.append(f"unfilled scaffold — the {SCAFFOLD_SENTINEL} marker is still present; implement "
                       "run() (and remove the marker) before registering")
    if execute:
        ok, detail = run_sibling_test_hermetic(py_path)
        if not ok:
            reasons.append(f"hermetic sibling test did not pass: {detail}")
    return GateResult(not reasons, "generated", reasons)


def library_items_complete(library_root: str | Path, *, execute: bool = False) -> list[str]:
    """The generation-completeness INVARIANT over a whole per-project library (S11b-3b / #630): every
    generated **tool** (a ``*.py`` under any ``**/skills`` dir of *library_root*) must pass
    :func:`gate_generated` — filled (no scaffold sentinel) + CASE/schema/sibling-test, and with
    ``execute=True`` its sibling test also passes hermetically; and every generated **prompt item** (a
    ``kind: prompt`` ``SKILL.md``) must pass :func:`gate_prompt` under ``strict_locales=True`` — a prompt
    that declares a language must actually ship that translation (S11b-1b: "declared == delivered").
    Returns a list of human-readable problems (empty ⇒ the library is complete). Pure / fail-soft: a
    missing root ⇒ ``[]``.

    This is the reusable invariant the **S17 self-dogfood acceptance** + an operator run against a real
    project library (a RUNTIME per-project tree). It is deliberately NOT registered in the dev-process
    scheduled reconciler (``process_doctor``): that reconciles the repo's GitHub/release state, whereas
    generated libraries live per-project at runtime — there is nothing for a repo-CI run to scan. The
    runtime enforcement is the loader, which drops an unfilled scaffold at load (S11b-3a)."""
    from ack.prompt import is_prompt_item

    root = Path(library_root)
    if not root.is_dir():
        return []
    problems: list[str] = []
    for skills_dir in sorted(root.glob("**/skills")):
        if not skills_dir.is_dir():
            continue
        for py in sorted(skills_dir.glob("*.py")):
            if py.stem.startswith("_"):
                continue
            res = gate_generated(py, execute=execute)
            if not res.passed:
                problems.append(f"{py.relative_to(root).as_posix()}: " + "; ".join(res.reasons))
    for skill_md in sorted(root.glob("**/SKILL.md")):
        if not is_prompt_item(skill_md):           # playbooks (kind: playbook) are not locale items
            continue
        res = gate_prompt(skill_md, strict_locales=True)
        if not res.passed:
            problems.append(f"{skill_md.relative_to(root).as_posix()}: " + "; ".join(res.reasons))
    return problems


def gate_tool(py_path: str | Path) -> GateResult:
    """Doctor preflight for a typed ``CASE``+``run`` skill file."""
    from ack.doctor import load_module_by_path, syntax_error
    from ack.registry import derive_tool_schema

    p = Path(py_path)
    reasons: list[str] = []
    if not p.is_file():
        return GateResult(False, "tool", [f"no such file: {p}"])
    se = syntax_error(p)
    if se:
        return GateResult(False, "tool", [f"syntax error: {se}"])
    try:
        mod = load_module_by_path(f"_gate_{p.stem}", p)
    except Exception as e:  # noqa: BLE001 — a load failure is a gate failure, surfaced
        return GateResult(False, "tool", [f"import failed: {e!r}"])

    case = getattr(mod, "CASE", None)
    if not isinstance(case, dict):
        reasons.append("no CASE dict")
    elif not str(case.get("capability") or "").strip():
        reasons.append("CASE has no non-empty 'capability'")
    run = getattr(mod, "run", None)
    if not callable(run):
        reasons.append("no callable run()")
    else:
        if inspect.iscoroutinefunction(run):
            reasons.append("run() must be synchronous (async not allowed on the tool path)")
        try:
            schema = derive_tool_schema(run)
            if not isinstance(schema, dict) or schema.get("type") != "object":
                reasons.append("run() does not yield a valid object tool schema")
        except Exception as e:  # noqa: BLE001
            reasons.append(f"tool schema not derivable: {e!r}")

    # "ships with auto-generated tests": a sibling tests/test_<stem>.py must exist
    test_file = p.parent.parent / "tests" / f"test_{p.stem}.py"
    if not test_file.is_file():
        reasons.append(f"no auto-generated test ({test_file.name}) — unchecked code")

    return GateResult(not reasons, "tool", reasons)


def gate_playbook(skill_md: str | Path, *, run_check: bool = True) -> GateResult:
    """Validate a playbook package: frontmatter schema + readable references + scripts/check."""
    from ack.playbook import PlaybookError, parse_playbook, validate_meta

    p = Path(skill_md)
    if p.is_dir():
        p = p / "SKILL.md"
    if not p.is_file():
        return GateResult(False, "playbook", [f"no SKILL.md at {p}"])
    reasons: list[str] = []
    try:
        pb = parse_playbook(p)
        reasons.extend(validate_meta(pb.meta))
    except PlaybookError as e:
        return GateResult(False, "playbook", [f"frontmatter invalid: {e}"])

    refs_dir = p.parent / "references"
    if refs_dir.is_dir():
        for ref in refs_dir.glob("*"):
            if ref.is_file() and ref.name != ".gitkeep":
                try:
                    ref.read_text(encoding="utf-8")
                except OSError as e:
                    reasons.append(f"reference {ref.name!r} unreadable: {e}")

    check = p.parent / "scripts" / "check"
    if run_check and check.is_file():
        try:
            cp = subprocess.run([sys.executable, str(check)], capture_output=True,
                                text=True, timeout=60)
            if cp.returncode != 0:
                reasons.append(f"scripts/check failed (rc={cp.returncode}): "
                               f"{(cp.stdout + cp.stderr).strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            reasons.append(f"scripts/check not runnable: {e!r}")

    return GateResult(not reasons, "playbook", reasons)


def gate_prompt(skill_md: str | Path, *, strict_locales: bool = False) -> GateResult:
    """Eval/registration gate for a ``kind: prompt`` item (#111).

    A prompt passes iff: its frontmatter validates (``ack.prompt`` schema), every **required**
    variable actually appears as a ``{placeholder}`` in the template (a required input that can't
    affect the output is a defect), and it **assembles cleanly in every declared language** —
    proving the `locales/<lang>.json` overlays are readable and well-formed. Deterministic, model-free.

    Locale strictness (#630 S11b-1b). By default a *missing* overlay for a declared non-source
    language is **fine** — it falls back to the English source (the lenient registration gate). With
    ``strict_locales=True`` a declared non-source language whose overlay is **absent** is a **failure**:
    a generated prompt that claims to speak a language must actually ship that translation ("declared ==
    delivered"). This is the completeness variant the per-project library invariant uses
    (:func:`library_items_complete`). The source language is English (``en`` needs no overlay); a
    *present* overlay is always validated under both modes (a malformed translation is a defect either
    way).
    """
    from ack.prompt import PromptError, parse_prompt
    from ack.promptgen import _PLACEHOLDER, assemble

    p = Path(skill_md)
    if p.is_dir():
        p = p / "SKILL.md"
    if not p.is_file():
        return GateResult(False, "prompt", [f"no SKILL.md at {p}"])
    try:
        prompt = parse_prompt(p)
    except PromptError as e:
        return GateResult(False, "prompt", [f"frontmatter invalid: {e}"])

    import json

    reasons: list[str] = []
    placeholders = set(_PLACEHOLDER.findall(prompt.template))
    for v in prompt.variables:
        if v.required and v.name not in placeholders:
            reasons.append(f"required variable {v.name!r} is never used in the template")

    sample = {v.name: f"<{v.name}>" for v in prompt.variables}
    for lang in prompt.languages:
        # A *missing* overlay is fine (intentional English fallback); a *present* one that is
        # malformed is a defect — the runtime would silently fall back, masking a broken
        # translation. The gate is where "assemblable in DE+EN" must actually mean DE works.
        if lang != "en":
            overlay = prompt.locales_dir() / f"{lang}.json"
            if overlay.is_file():
                try:
                    data = json.loads(overlay.read_text(encoding="utf-8"))
                    template = data.get("template") if isinstance(data, dict) else None
                    if not isinstance(data, dict):
                        reasons.append(f"{lang!r} overlay {overlay.name} is not a JSON object")
                    elif not isinstance(template, str) or not template.strip():
                        # a non-string template (e.g. a number) would be silently ignored by the
                        # Localizer at runtime → a present-but-useless overlay masquerading as a
                        # translation; require an actual non-empty string under both modes.
                        reasons.append(f"{lang!r} overlay {overlay.name} has no non-empty 'template' string")
                except (OSError, ValueError) as e:
                    reasons.append(f"{lang!r} overlay {overlay.name} is unreadable/invalid JSON: {e}")
            elif strict_locales:
                reasons.append(f"declared language {lang!r} has no overlay {overlay.name} "
                               f"(strict: every declared language must ship a translation)")
        try:
            assemble(prompt, sample, lang=lang)   # all vars provided → strict is fine
        except Exception as e:  # noqa: BLE001 — any assembly failure is a gate failure, surfaced
            reasons.append(f"not assemblable in {lang!r}: {e!r}")

    return GateResult(not reasons, "prompt", reasons)


def gate(path: str | Path, **kw) -> GateResult:
    """Dispatch to the right gate by item kind/path shape: ``kind: prompt`` SKILL.md → prompt;
    other SKILL.md/dir → playbook; ``.py`` → tool."""
    from ack.prompt import is_prompt_item

    p = Path(path)
    if p.is_dir() or p.name == "SKILL.md":
        md = p / "SKILL.md" if p.is_dir() else p
        if md.is_file() and is_prompt_item(md):
            return gate_prompt(md)
        return gate_playbook(p, **kw)
    return gate_tool(p)
