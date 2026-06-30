from __future__ import annotations

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


def test_generate_creates_files_into_output_root(tmp_path):
    ctx = _demo_widget_ctx()
    assert ctx["capability_key"] == "p-widget"
    out = tmp_path / "lib"
    res = g.generate(ctx, output_root=out)
    assert res.ok
    assert res.files
    assert res.domain_dir is not None
    assert any(out.rglob("*"))


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
