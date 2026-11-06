import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

from .progress import stage
from .storage import write_json


def compile_pdf(source: Path):
    """Compile saved sources, retaining the TeX and diagnostics on failure."""
    source = source.resolve()
    if not source.is_file() or source.suffix != ".tex":
        raise ValueError(f"TeX source not found: {source}")
    executable = shutil.which("latexmk")
    if not executable:
        raise RuntimeError("PDF生成にはlatexmkとLuaLaTeXが必要です（TeX Live / MacTeX）。")
    target = source.with_suffix(".pdf")
    log = source.parent / "compile.log"
    with stage("PDFを生成", detail=source.name), tempfile.TemporaryDirectory(prefix="moodle-tex-") as tmp:
        command = [
            executable,
            "-norc",
            "-lualatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            "-no-shell-escape",
            f"-outdir={tmp}",
            source.name,
        ]
        try:
            with log.open("w") as output:
                proc = subprocess.run(
                    command,
                    cwd=source.parent,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=180,
                    check=False,
                )
            built = Path(tmp) / target.name
            if proc.returncode != 0 or not built.exists():
                raise RuntimeError(f"PDF生成に失敗しました。TeXは保存済みです。ログ: {log}")
            pages = len(PdfReader(built).pages)
            if pages == 0:
                raise RuntimeError("生成PDFにページがありません")
            shutil.copy2(built, target)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"PDF生成が180秒でタイムアウトしました。ログ: {log}") from None
    write_json(
        source.parent / "compile.json",
        {
            "pdf": target.name,
            "pages": pages,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
    )
    return target
