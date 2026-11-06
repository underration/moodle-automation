from types import SimpleNamespace

import pytest

from moodle_automation.latex import compile_pdf


def test_compile_failure_preserves_source_and_log(tmp_path, monkeypatch):
    source = tmp_path / "main.tex"
    source.write_text("invalid source")
    monkeypatch.setattr("moodle_automation.latex.shutil.which", lambda _: "/fake/latexmk")

    def fail(command, **kwargs):
        assert "-no-shell-escape" in command and "-norc" in command
        kwargs["stdout"].write("TeX compilation error")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("moodle_automation.latex.subprocess.run", fail)
    with pytest.raises(RuntimeError, match="PDF生成に失敗"):
        compile_pdf(source)
    assert source.read_text() == "invalid source"
    assert "compilation error" in (tmp_path / "compile.log").read_text()
    assert not source.with_suffix(".pdf").exists()
