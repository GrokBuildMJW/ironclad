"""`/cat` fences file content with the language from the extension (MEM-20).

So markdown clients render it as preserved, syntax-highlighted code instead of reflowing it as
prose. Read errors stay unfenced (they are a message, not code).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402


def test_lang_for_path():
    assert gx10._lang_for_path("a/b/main.py") == "python"
    assert gx10._lang_for_path("app.ts") == "typescript"
    assert gx10._lang_for_path("data.json") == "json"
    assert gx10._lang_for_path("Dockerfile") == "dockerfile"
    assert gx10._lang_for_path("Makefile") == "makefile"
    assert gx10._lang_for_path("notes.unknownext") == ""
    assert gx10._lang_for_path("noextension") == ""


# manual_cat doesn't touch `self` (only run_tool + _lang_for_path), so call it unbound — no agent
# construction, which avoids depending on whichever openai stub another test left in sys.modules.
_cat = gx10.GX10.manual_cat


def test_manual_cat_fences_with_language(tmp_path):
    f = tmp_path / "snippet.py"
    body = "def f():\n        return 1  # deep indent kept\n"
    f.write_text(body, encoding="utf-8")
    out = _cat(None, str(f))
    assert out.startswith("```python\n"), out[:40]
    assert out.rstrip().endswith("```")
    assert body in out  # content verbatim (indentation preserved inside the fence)


def test_manual_cat_unknown_ext_fences_without_language(tmp_path):
    f = tmp_path / "plain.unknownext"
    f.write_text("hello\n", encoding="utf-8")
    out = _cat(None, str(f))
    assert out.startswith("```\n")  # fence with no language tag
    assert "hello" in out


def test_manual_cat_error_is_not_fenced(tmp_path):
    out = _cat(None, str(tmp_path / "does-not-exist.py"))
    assert "```" not in out  # an error message, not code
    assert "ERROR" in out or "error" in out.lower()
