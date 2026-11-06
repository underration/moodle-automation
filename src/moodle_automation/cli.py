import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .agent import build_context, draft, write_draft
from .config import Config
from .latex import compile_pdf
from .materials import collect
from .moodle import LoginRequired, Moodle, login
from .progress import Reporter, stage, track
from .storage import lock, write_json


def process(config, client, catalog, args):
    results = []
    selected = [
        (course, assignment)
        for course in catalog["courses"]
        for assignment in course["assignments"]
        if not args.assignment or assignment["id"] in args.assignment
    ]
    for course, assignment in track(selected, "課題を処理", name=lambda pair: pair[1]["name"]):
        aid = assignment["id"]
        if assignment["submitted"] is True and not args.include_submitted:
            results.append({"id": aid, "status": "skipped_submitted"})
            continue
        if assignment["submitted"] is None:
            results.append({"id": aid, "status": "needs_review", "reason": "Unknown submission status"})
            continue
        if not args.include_overdue and assignment["due_at"]:
            if datetime.fromisoformat(assignment["due_at"]) < datetime.now(ZoneInfo(config.timezone)):
                results.append({"id": aid, "status": "skipped_overdue"})
                continue
        print(f"課題 {aid}: {assignment['name']} — 資料を取得中", flush=True)
        root = config.output_dir / str(course["id"]) / str(aid)
        root.mkdir(parents=True, exist_ok=True)
        try:
            sources, warnings = collect(client, course, assignment, root / "materials")
            if not assignment["due_raw"]:
                warnings.append("Due date is not available; verify in Moodle")
            if not sources:
                warnings.append("No lecture material could be extracted")
            context = build_context(config, assignment, sources, warnings)
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"version": 1, "model": config.model, "context": context},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:20]
            final = root / fingerprint
            receipt = final / "result.json"
            if receipt.exists() and not args.regenerate:
                existing = json.loads(receipt.read_text())
                if existing.get("output") is None or (final / existing["output"]).exists():
                    if existing.get("output", "") == "main.tex":
                        compile_pdf(final / "main.tex")
                        existing["pdf"] = "main.pdf"
                        existing["status"] = json.loads((final / "draft.json").read_text())["status"]
                        existing.pop("reason", None)
                        write_json(receipt, existing)
                    results.append({"id": aid, "status": "unchanged", "directory": str(final)})
                    continue
            # New revision even when the user requests regeneration or an existing file was removed.
            if final.exists():
                final = root / (fingerprint + "-" + datetime.now().strftime("%Y%m%dT%H%M%S%f"))
            print(f"課題 {aid}: Codexで草案を作成中", flush=True)
            with stage("GPTで草案を生成", detail=f"応答待ち・上限 {config.codex_timeout_seconds}秒"):
                answer = draft(config, context)
            with tempfile.TemporaryDirectory(dir=root, prefix=".draft-") as temporary:
                staging = Path(temporary)
                output = write_draft(staging, answer)
                write_json(staging / "assignment.json", assignment)
                write_json(staging / "sources.json", sources)
                write_json(staging / "context.json", context)
                write_json(staging / "draft.json", answer)
                (staging / "REVIEW.md").write_text(
                    "# 草案の確認事項\n\n"
                    + answer["format_reason"]
                    + "\n\n"
                    + "\n".join("- " + n for n in answer["review_notes"])
                    + "\n\n自動提出は行っていません。PDF生成結果はcompile.json、ログはcompile.logを確認してください。内容・レイアウトの最終確認は必要です。\n"
                )
                record = {
                    "id": aid,
                    "status": answer["status"],
                    "output": output.name if output else None,
                    "fingerprint": fingerprint,
                    "directory": str(final),
                }
                write_json(staging / "result.json", record)
                staging.rename(final)
            if record["output"] == "main.tex":
                try:
                    compile_pdf(final / "main.tex")
                    record["pdf"] = "main.pdf"
                except RuntimeError as exc:
                    record.update(status="failed", reason=str(exc))
                    write_json(final / "result.json", record)
                    raise
                write_json(final / "result.json", record)
            results.append(record)
            print(f"保存: {final}", flush=True)
        except LoginRequired:
            raise
        except Exception as exc:
            results.append({"id": aid, "status": "failed", "error": type(exc).__name__, "reason": str(exc)})
            print(f"課題 {aid}: 失敗 ({type(exc).__name__})。次回再実行できます。", file=sys.stderr)
    return results


def main():
    parser = argparse.ArgumentParser(description="Moodle → 講義資料 → Codex草案（提出は行いません）")
    parser.add_argument("--config", type=Path, default=Path("moodle.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="専用ブラウザでMoodleにログインしてセッション保存")
    sub.add_parser("doctor", help="設定・依存・ChatGPTログインの確認")
    sub.add_parser("scan", help="課題と提出状況の検出のみ")
    compiler = sub.add_parser("compile", help="保存済みのTeXをPDFにコンパイル")
    compiler.add_argument("source", type=Path)
    run = sub.add_parser("run", help="未提出課題の資料取得と草案生成")
    run.add_argument("--assignment", type=int, action="append", help="対象課題ID（複数回指定可能）")
    run.add_argument("--include-submitted", action="store_true")
    run.add_argument("--include-overdue", action="store_true")
    run.add_argument("--regenerate", action="store_true", help="上書きせず新しい版を作成")
    args = parser.parse_args()
    try:
        if args.command == "compile":
            with Reporter():
                print(f"PDF: {compile_pdf(args.source)}")
            return 0
        config = Config.load(args.config)
        if args.command == "doctor":
            print(f"Moodle: {config.base_url}\nOutput: {config.output_dir}")
            print(
                "Moodle session:",
                "saved" if (config.state_dir / "session.json").exists() else "login required",
            )
            if not shutil.which("codex"):
                raise RuntimeError("Install Codex CLI first")
            result = subprocess.run(["codex", "login", "status"], capture_output=True, text=True)
            status = result.stdout + result.stderr
            print("Codex:", "ChatGPT login available" if "ChatGPT" in status else "Run codex login")
            return 0 if result.returncode == 0 and "ChatGPT" in status else 1
        with lock(config.state_dir), Reporter():
            if args.command == "login":
                with stage("Moodleログイン待ち", detail="専用ブラウザでログインしてください（最大10分）"):
                    login(config)
                return 0
            config.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            config.output_dir.chmod(0o700)
            client = Moodle(config)
            try:
                catalog = client.scan()
                write_json(config.state_dir / "catalog.json", catalog)
                for course in catalog["courses"]:
                    for a in course["assignments"]:
                        print(
                            f"{a['id']} | {a['course_name']} | {a['name']} | {a['submission_status']} | {a['due_raw']}"
                        )
                if args.command == "scan":
                    print(f"一覧: {config.state_dir / 'catalog.json'}")
                    return 1 if catalog["failures"] else 0
                if args.assignment:
                    found = {a["id"] for c in catalog["courses"] for a in c["assignments"]}
                    missing = set(args.assignment) - found
                    if missing:
                        raise ValueError(f"Assignment IDs not discovered: {sorted(missing)}")
                results = process(config, client, catalog, args)
                write_json(
                    config.state_dir / "last-run.json",
                    {"results": results, "scan_failures": catalog["failures"]},
                )
                for r in results:
                    print(f"{r['id']}: {r['status']}")
                return 1 if catalog["failures"] or any(r["status"] == "failed" for r in results) else 0
            finally:
                client.close()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"停止: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
