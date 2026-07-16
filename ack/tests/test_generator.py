from __future__ import annotations

import ast
import json
from pathlib import Path

from ack import generator as g
from ack.gate import gate_prompt
from ack.prompt import parse_prompt
import pytest


def _demo_widget_ctx():
    parser = g.build_parser()
    args = parser.parse_args(
        ["--domain", "demo", "--case", "widget", "--description", "x", "--prefix", "p"]
    )
    return g.build_context(args)


def _template(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _tree_snapshot(root: Path):
    if not root.exists():
        return None
    return {
        p.relative_to(root).as_posix(): None if p.is_dir() else p.read_bytes()
        for p in sorted(root.rglob("*"))
    }


def test_generate_creates_files_into_output_root(tmp_path):
    ctx = _demo_widget_ctx()
    assert ctx["capability_key"] == "p-widget"
    out = tmp_path / "lib"
    res = g.generate(ctx, output_root=out)
    assert res.ok
    assert res.files
    assert res.domain_dir is not None
    assert any(out.rglob("*"))


@pytest.mark.parametrize("failure", ["target", "state"])
def test_generate_commit_failure_rolls_back_entire_tree_and_state(tmp_path, monkeypatch, failure):
    ctx = _demo_widget_ctx()
    template = _template(
        tmp_path / "template",
        {"{{ domain_folder }}/a.txt": "old a\n", "{{ domain_folder }}/b.txt": "old b\n"},
    )
    out = tmp_path / "lib"
    state_path = out / "Demo" / g.STATE_FILENAME

    if failure == "state":
        g.generate(ctx, template_root=template, output_root=out)
        _template(
            template,
            {"{{ domain_folder }}/a.txt": "new a\n", "{{ domain_folder }}/b.txt": "new b\n"},
        )

    before = _tree_snapshot(out)
    state_before = state_path.read_bytes() if state_path.exists() else None
    original_write_text = Path.write_text
    raised = False

    def fail_commit_write(path, content, *args, **kwargs):
        nonlocal raised
        should_fail = path == (out / "Demo" / "b.txt") if failure == "target" else path == state_path
        if should_fail and not raised:
            raised = True
            raise OSError("commit failed")
        return original_write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_commit_write)
    with pytest.raises(OSError, match="commit failed"):
        g.generate(ctx, template_root=template, output_root=out)

    assert raised
    assert _tree_snapshot(out) == before
    assert (state_path.read_bytes() if state_path.exists() else None) == state_before


def test_generate_rollback_is_best_effort_and_preserves_original_error(tmp_path, monkeypatch):
    ctx = _demo_widget_ctx()
    template = _template(
        tmp_path / "template",
        {"{{ domain_folder }}/a.txt": "old a\n", "{{ domain_folder }}/b.txt": "old b\n"},
    )
    out = tmp_path / "lib"
    g.generate(ctx, template_root=template, output_root=out)
    _template(
        template,
        {"{{ domain_folder }}/a.txt": "new a\n", "{{ domain_folder }}/b.txt": "new b\n"},
    )
    state_path = out / "Demo" / g.STATE_FILENAME
    state_before = state_path.read_bytes()
    original_write_text = Path.write_text
    original_write_bytes = Path.write_bytes

    def fail_state_write(path, content, *args, **kwargs):
        if path == state_path:
            raise OSError("original commit error")
        return original_write_text(path, content, *args, **kwargs)

    def fail_one_rollback(path, content):
        if path == out / "Demo" / "b.txt":
            raise OSError("secondary rollback error")
        return original_write_bytes(path, content)

    monkeypatch.setattr(Path, "write_text", fail_state_write)
    monkeypatch.setattr(Path, "write_bytes", fail_one_rollback)
    with pytest.raises(OSError, match="original commit error"):
        g.generate(ctx, template_root=template, output_root=out)

    assert (out / "Demo" / "a.txt").read_text(encoding="utf-8") == "old a\n"
    assert (out / "Demo" / "b.txt").read_text(encoding="utf-8") == "new b\n"
    assert state_path.read_bytes() == state_before


def test_generate_commits_matching_state_and_identical_rerun_writes_no_targets(tmp_path, monkeypatch):
    ctx = _demo_widget_ctx()
    template = _template(
        tmp_path / "template",
        {"{{ domain_folder }}/a.txt": "alpha\n", "{{ domain_folder }}/nested/b.txt": "beta\n"},
    )
    out = tmp_path / "lib"
    first = g.generate(ctx, template_root=template, output_root=out)
    state_path = out / "Demo" / g.STATE_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert first.ok
    assert state["files"]
    assert {rel: (out / Path(rel)).read_text(encoding="utf-8") for rel in state["files"]} == state["files"]

    writes: list[Path] = []
    original_write_text = Path.write_text

    def record_write(path, content, *args, **kwargs):
        writes.append(path)
        return original_write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", record_write)
    second = g.generate(ctx, template_root=template, output_root=out)
    assert second.files and all(file.action == "unchanged" for file in second.files)
    assert writes == [state_path]


def test_generate_dry_run_changes_neither_new_nor_existing_tree(tmp_path):
    ctx = _demo_widget_ctx()
    template = _template(tmp_path / "template", {"{{ domain_folder }}/a.txt": "alpha\n"})
    new_out = tmp_path / "new-lib"
    g.generate(ctx, template_root=template, output_root=new_out, dry_run=True)
    assert not new_out.exists()

    existing_out = tmp_path / "existing-lib"
    g.generate(ctx, template_root=template, output_root=existing_out)
    before = _tree_snapshot(existing_out)
    _template(template, {"{{ domain_folder }}/a.txt": "upgraded\n"})
    result = g.generate(ctx, template_root=template, output_root=existing_out, dry_run=True)
    assert any(file.action == "upgraded" for file in result.files)
    assert _tree_snapshot(existing_out) == before


def test_generate_first_run_then_upgrade_merges_local_edits_and_updates_base(tmp_path):
    ctx = _demo_widget_ctx()
    template = _template(
        tmp_path / "template",
        {"{{ domain_folder }}/a.txt": "title old\nstable anchor\nbody original\n"},
    )
    out = tmp_path / "lib"
    first = g.generate(ctx, template_root=template, output_root=out)
    target = out / "Demo" / "a.txt"
    state_path = out / "Demo" / g.STATE_FILENAME

    assert [file.action for file in first.files] == ["created"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["files"]["Demo/a.txt"] == (
        "title old\nstable anchor\nbody original\n"
    )

    target.write_text("title old\nstable anchor\nbody local\n", encoding="utf-8")
    _template(template, {"{{ domain_folder }}/a.txt": "title new\nstable anchor\nbody original\n"})
    upgrade = g.generate(ctx, template_root=template, output_root=out)

    assert [file.action for file in upgrade.files] == ["upgraded"]
    assert target.read_text(encoding="utf-8") == "title new\nstable anchor\nbody local\n"
    assert json.loads(state_path.read_text(encoding="utf-8"))["files"]["Demo/a.txt"] == (
        "title new\nstable anchor\nbody original\n"
    )


def test_skipped_untracked_file_records_no_phantom_baseline(tmp_path):
    # GEN-2 (#503): a SKIPPED untracked file must NOT get a baseline — recording the render as the base
    # made the NEXT run three-way-merge the user's declined file against a phantom base (spurious diff3
    # conflicts / a silent merge instead of a clean skip).
    ctx = _demo_widget_ctx()
    out = tmp_path / "lib"
    g.generate(ctx, output_root=out)                                   # run A: create + record state
    for st in out.rglob(g.STATE_FILENAME):
        st.unlink()                                                    # forget state → files now untracked
    run_b = g.generate(ctx, output_root=out)                          # run B: all skipped (untracked, no --force)
    assert run_b.files and all(f.action == "skipped" for f in run_b.files)
    run_c = g.generate(ctx, output_root=out)                          # run C: STILL skipped — no phantom baseline
    assert all(f.action == "skipped" for f in run_c.files)            # pre-fix: "unchanged" (merged vs phantom base)


def test_copier_yml_declares_every_template_token():
    # TPL-1 (#503): every {{ token }} the rendered tree references (file contents AND path segments) must be
    # declared in copier.yml — else a raw `copier copy` fails on StrictUndefined (date/tags_yaml/tags_csv
    # were missing; the ack.generator CLI filled them via build_context, masking it).
    import re
    args = g.build_parser().parse_args(["--domain", "d", "--case", "c", "--description", "x"])
    root = Path(g.template_root_for(args))
    cyml = (root / "copier.yml").read_text(encoding="utf-8")
    declared = set(re.findall(r"(?m)^([a-z][a-z0-9_]*):", cyml))   # column-0 question/computed keys
    tok = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)")
    meta = {"copier.yml", "copier.yaml", "TEMPLATE-README.md"}
    used: set[str] = set()
    for p in root.rglob("*"):
        used |= set(tok.findall(str(p.relative_to(root))))         # path-segment tokens ({{domain_folder}} …)
        if p.is_file() and p.name not in meta:
            used |= set(tok.findall(p.read_text(encoding="utf-8")))
    missing = used - declared
    assert not missing, f"copier.yml is missing template tokens (raw `copier copy` would fail): {sorted(missing)}"


def test_copier_non_negotiable_is_a_lowercase_json_string():
    # TPL-2 (#503): non_negotiable is embedded in a JSON object, so it must render lowercase true/false —
    # a copier `bool` emits True/False (invalid JSON). It is now a when:false computed string.
    args = g.build_parser().parse_args(["--domain", "d", "--case", "c", "--description", "x"])
    cyml = (Path(g.template_root_for(args)) / "copier.yml").read_text(encoding="utf-8")
    assert "'true' if non_negotiable_flag else 'false'" in cyml


def test_reserved_capability_is_refused_nothing_written(tmp_path):
    ctx = _demo_widget_ctx()
    assert ctx["capability_key"] == "p-widget"
    out = tmp_path / "lib"
    res = g.generate(ctx, output_root=out, reserved_capabilities={"p-widget"})
    assert res.refused
    assert not res.ok
    assert res.files == []
    assert not out.exists() or list(out.rglob("*")) == []


def test_non_reserved_capability_generates(tmp_path):
    ctx = _demo_widget_ctx()
    out = tmp_path / "lib"
    res = g.generate(ctx, output_root=out, reserved_capabilities={"something-else"})
    assert res.ok
    assert res.files
    assert not res.refused


def test_no_reserved_set_is_unguarded(tmp_path):
    ctx = _demo_widget_ctx()
    res = g.generate(ctx, output_root=tmp_path / "lib", reserved_capabilities=None)
    assert res.ok
    assert not res.refused


def test_cli_refuses_reserved_capability(tmp_path):
    rc = g.main(
        [
            "--domain",
            "demo",
            "--case",
            "widget",
            "--description",
            "x",
            "--prefix",
            "p",
            "--output-root",
            str(tmp_path / "c"),
            "--reserved-capabilities",
            "p-widget",
        ]
    )
    assert rc == 2


def test_cli_generates_when_not_reserved(tmp_path):
    rc = g.main(
        [
            "--domain",
            "demo",
            "--case",
            "widget",
            "--description",
            "x",
            "--prefix",
            "p",
            "--output-root",
            str(tmp_path / "c"),
            "--reserved-capabilities",
            "other-cap",
        ]
    )
    assert rc == 0


def _prompt_args(tmp_path, **over):
    a = dict(domain="writing", case="blog-brief", description="Draft a brief", kind="prompt")
    a.update(over)
    argv = ["--kind", a["kind"], "--domain", a["domain"], "--case", a["case"], "--description", a["description"]]
    return g.build_parser().parse_args(argv)


def test_template_root_for_defaults_to_case():
    args = g.build_parser().parse_args(["--domain", "d", "--case", "c", "--description", "x"])
    assert g.template_root_for(args) == g.DEFAULT_TEMPLATE and args.kind == "case"


def test_template_root_for_prompt_selects_new_prompt():
    args = g.build_parser().parse_args(["--kind", "prompt", "--domain", "d", "--case", "c", "--description", "x"])
    assert g.template_root_for(args) == g.PROMPT_TEMPLATE


def test_template_root_for_explicit_template_overrides_both_kinds():
    for kind in ("case", "prompt"):
        args = g.build_parser().parse_args(
            ["--kind", kind, "--domain", "d", "--case", "c", "--description", "x", "--template", "/some/dir"]
        )
        assert g.template_root_for(args) == Path("/some/dir")


def test_kind_prompt_generates_gate_valid_item(tmp_path):
    args = _prompt_args(tmp_path)
    ctx = g.build_context(args)
    res = g.generate(ctx, template_root=g.template_root_for(args), output_root=tmp_path)
    assert res.ok
    skill = tmp_path / "Writing" / "blog-brief" / "SKILL.md"
    assert skill.exists()
    gr = gate_prompt(skill)
    assert gr.passed, gr.reasons
    p = parse_prompt(skill)
    assert p.capability == "w-blog-brief"
    assert "de" in p.languages
    assert (tmp_path / "Writing" / "blog-brief" / "locales" / "de.json").exists()


def test_kind_prompt_is_rerunnable_noop(tmp_path):
    args = _prompt_args(tmp_path)
    ctx = g.build_context(args)
    res1 = g.generate(ctx, template_root=g.template_root_for(args), output_root=tmp_path)
    assert res1.ok
    res2 = g.generate(ctx, template_root=g.template_root_for(args), output_root=tmp_path)
    assert res2.ok
    assert res2.conflicts == 0
    assert res2.files
    assert all(f.action == "unchanged" for f in res2.files)


def test_kind_case_default_still_renders_case_tree(tmp_path):
    args = g.build_parser().parse_args(["--domain", "demo", "--case", "widget", "--description", "x", "--prefix", "p"])
    ctx = g.build_context(args)
    res = g.generate(ctx, template_root=g.template_root_for(args), output_root=tmp_path)
    assert res.ok
    assert (tmp_path / "Demo" / "skills" / "widget.py").exists()
    assert (tmp_path / "Demo" / "widget-spec.md").exists()
    assert not list(tmp_path.rglob("SKILL.md"))


def test_kind_case_default_output_byte_identical_to_explicit_template(tmp_path):
    # the --kind case default must render EXACTLY what the explicit new-case --template renders,
    # byte-for-byte (not merely the same tree shape) — the resolver may not perturb the case path.
    argv = ["--domain", "demo", "--case", "widget", "--description", "x", "--prefix", "p"]
    a = g.build_parser().parse_args(argv)
    b = g.build_parser().parse_args(argv + ["--template", str(g.DEFAULT_TEMPLATE)])
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    g.generate(g.build_context(a), template_root=g.template_root_for(a), output_root=out_a)
    g.generate(g.build_context(b), template_root=g.template_root_for(b), output_root=out_b)
    files_a = sorted(p.relative_to(out_a).as_posix() for p in out_a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(out_b).as_posix() for p in out_b.rglob("*") if p.is_file())
    assert files_a == files_b and files_a
    for rel in files_a:
        assert (out_a / rel).read_bytes() == (out_b / rel).read_bytes(), rel


def test_kind_prompt_assembles_in_de_with_substituted_input(tmp_path):
    # the generated DE overlay must actually substitute {input} and differ from the English source.
    from ack.promptgen import assemble

    args = _prompt_args(tmp_path)
    g.generate(g.build_context(args), template_root=g.template_root_for(args), output_root=tmp_path)
    p = parse_prompt(tmp_path / "Writing" / "blog-brief" / "SKILL.md")
    out_de = assemble(p, {"input": "MARKER-XYZ"}, lang="de")
    out_en = assemble(p, {"input": "MARKER-XYZ"}, lang="en")
    assert "MARKER-XYZ" in out_de
    assert "MARKER-XYZ" in out_en
    assert out_de != out_en  # the German overlay is a real translation, not the source verbatim


# ── #1533: per-context escaping so an operator description can't break the generated syntax ────────────
from ack.playbook import _coerce_scalar, parse_frontmatter   # the SHIPPED frontmatter consumer

_NASTY = 'Summarize "VIP" rows'   # unescaped, this breaks quoted Python/JSON
_UNICODE = 'Résumé "café" — 100%'  # non-ASCII + quotes; must round-trip literally (ensure_ascii=False)


def test_tojson_renders_valid_python_json_and_round_trips_through_the_frontmatter_reader():
    for value in (_NASTY, _UNICODE):
        ctx = {"description": value}
        # a quoted-Python / quoted-JSON sink drops its hand-written quotes; tojson supplies a full quoted scalar
        line = g.render_str('    "description": {{description|tojson}},', ctx)
        ast.parse("_ = {\n" + line + "\n}")                   # a valid Python dict entry (compiles)
        row = g.render_str('{"notes": {{description|tojson}}}', ctx)
        assert json.loads(row)["notes"] == value             # a valid JSON object, round-trips
        # the YAML frontmatter scalar must round-trip through the SHIPPED reader (_coerce_scalar), not just
        # be a byte string — a raw `s[1:-1]` strip would corrupt the escaped quotes (the Grok-caught defect)
        yaml_line = g.render_str("description: {{description|tojson}}", ctx)
        assert _coerce_scalar(yaml_line.split(":", 1)[1].strip()) == value


def test_tojson_is_byte_identical_for_an_ordinary_value():
    # moving the quotes into the filter must not change output for a plain description (no drift on the paved road)
    ctx = {"description": "Summarize rows"}
    assert g.render_str('    "description": {{description|tojson}},', ctx) == '    "description": "Summarize rows",'


def test_render_filterless_token_is_byte_identical():
    # the escaping is opt-in per placeholder — a filterless token behaves exactly as before
    assert g.render_str("x {{description}} y", {"description": _NASTY}) == "x " + _NASTY + " y"


def test_nasty_description_renders_a_syntactically_valid_case_scaffold(tmp_path):
    # the end-to-end repro: a quoted description must produce a compilable skill and a parseable MAPPING row
    parser = g.build_parser()
    args = parser.parse_args(
        ["--domain", "audit-demo", "--case", "vip-rows", "--description", _NASTY, "--prefix", "p"])
    ctx = g.build_context(args)
    out = tmp_path / "lib"
    res = g.generate(ctx, template_root=g.template_root_for(args), output_root=out)
    assert res.ok
    skill = next(out.rglob("skills/*.py"))
    ast.parse(skill.read_text(encoding="utf-8"))              # was: SyntaxError on the unescaped quote
    gap = next(out.rglob("*-gap-tracking.md"))
    rows = [ln.strip() for ln in gap.read_text(encoding="utf-8").splitlines() if ln.strip().startswith('{"key"')]
    assert rows, "expected a MAPPING JSON row"
    for r in rows:
        assert json.loads(r)["notes"] == _NASTY              # was: JSONDecodeError on the unescaped quote


def test_nasty_description_renders_a_valid_prompt_scaffold(tmp_path):
    # the prompt (YAML frontmatter) sink: parse_prompt must recover the exact description, quotes and all
    from ack.prompt import parse_prompt
    parser = g.build_parser()
    args = parser.parse_args(
        ["--domain", "writing", "--case", "blog", "--description", _NASTY, "--kind", "prompt", "--prefix", "w"])
    out = tmp_path / "lib"
    g.generate(g.build_context(args), template_root=g.template_root_for(args), output_root=out)
    skill_md = next(out.rglob("SKILL.md"))
    meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    assert meta["description"] == _NASTY                      # was: corrupted by the literal-strip reader
    assert parse_prompt(skill_md).description == _NASTY       # and through the full prompt parser
