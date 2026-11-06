import json
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from moodle_automation.agent import build_context, draft, write_draft
from moodle_automation.cli import process
from moodle_automation.config import Config
from moodle_automation.materials import collect, safe_name
from moodle_automation.moodle import LoginRequired, Moodle
from moodle_automation.parsing import authenticated, parse_assignment, parse_course
from moodle_automation.storage import write_json

BASE = "https://lms.example.edu"
COURSE_HTML = """<h1>Networks</h1><div id="region-main"><li class="section"><h3>Lecture 4</h3>
<a href="/mod/resource/view.php?id=40">Slides</a>
<a href="/mod/assign/view.php?id=50">Report 4</a>
<a href="/mod/assign/view.php?id=50">Report 4 duplicate</a></li></div>"""
ASSIGNMENT_HTML = """<h1>Report 4</h1><div data-region="activity-dates"><div class="date-item">
<span class="date-item-label">期限:</span>2026年 11月 13日(金曜日) 00:00</div></div>
<div id="intro">Explain MAC. Submit Word.<a href="/pluginfile.php/1/mod_assign/intro/spec.pdf">spec</a></div>
<div class="submissionstatustable"><table><tr><th>提出ステータス</th>
<td class="submissionstatusnew">まだ提出されていません。</td></tr>
<tr><td><a href="/pluginfile.php/1/assignsubmission_file/submission_files/old.pdf">old answer</a></td></tr>
</table></div>"""


@pytest.fixture
def config(tmp_path):
    return Config(BASE, tmp_path / "state", tmp_path / "output")


def data():
    course = parse_course(COURSE_HTML, BASE + "/course/view.php?id=7")
    a = parse_assignment(ASSIGNMENT_HTML, course["activities"][1], course, "Asia/Tokyo")
    course["assignments"] = [a]
    return course, a


def answer(fmt="tex"):
    return {
        "status": "draft",
        "format": fmt,
        "format_reason": "Default TeX",
        "title": "Test",
        "content": r"\documentclass{article}\begin{document}Example\end{document}",
        "sections": [{"heading": "Test", "paragraphs": ["A paragraph"]}],
        "review_notes": [],
        "source_ids": [],
    }


def test_course_and_assignment_extract_separate_submission_files():
    c, a = data()
    assert len(c["activities"]) == 2
    assert a["section"] == "Lecture 4"
    assert a["due_at"] == "2026-11-13T00:00:00+09:00"
    assert a["submitted"] is False
    assert len(a["attachments"]) == 1
    assert "intro/spec.pdf" in a["attachments"][0]["url"]


@pytest.mark.parametrize(
    "status,expected", [("submitted", True), ("draft", False), ("new", False), ("xyz", None)]
)
def test_status_not_guessed(status, expected):
    c, _ = data()
    html = ASSIGNMENT_HTML.replace("submissionstatusnew", "submissionstatus" + status).replace(
        "まだ提出されていません。", "unknown"
    )
    assert parse_assignment(html, c["activities"][1], c, "Asia/Tokyo")["submitted"] is expected


def test_login_form_is_not_authenticated():
    assert not authenticated('<div class="usermenu"></div><input type="password">')
    assert authenticated('<button id="user-menu-toggle">user</button>')


def test_unsubmitted_without_status_class():
    course, _ = data()
    html = ASSIGNMENT_HTML.replace('class="submissionstatusnew"', 'class="cell c1 lastcol"')
    parsed = parse_assignment(html, course["activities"][1], course, "Asia/Tokyo")
    assert parsed["submitted"] is False
    assert parsed["submission_status"] == "まだ提出されていません。"


def make_client(config):
    write_json(
        config.state_dir / "session.json",
        {
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": "TEST",
                    "domain": "lms.example.edu",
                    "path": "/",
                    "expires": -1,
                }
            ]
        },
    )
    return Moodle(config)


def test_external_redirect_stops_before_second_request(config, monkeypatch):
    client = make_client(config)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(
            status_code=302, headers={"Location": "https://other.example/x"}, close=lambda: None
        )

    monkeypatch.setattr(client.session, "get", get)
    with pytest.raises(LoginRequired), client.get(BASE + "/file"):
        pass
    assert calls == [BASE + "/file"]
    assert not client.allowed("https://lms.example.edu.attacker.com/x")
    assert not client.allowed("http://lms.example.edu/x")


def test_download_size_limit(config):
    config.max_file_mb = 1
    client = make_client(config)
    response = SimpleNamespace(iter_content=lambda n: iter([b"x" * 1048577]))
    with pytest.raises(RuntimeError, match="exceeded"):
        client.read(response)


def test_resource_follows_file_and_extracts_text(config, tmp_path):
    course, a = data()
    a["attachments"] = []

    class Client:
        def allowed(self, url):
            return url.startswith(BASE)

        @contextmanager
        def get(self, url):
            if "/mod/resource/" in url:
                yield SimpleNamespace(
                    url=url,
                    headers={"Content-Type": "text/html"},
                    data=b'<div id="region-main"><a href="/pluginfile.php/1/mod_resource/content/lecture.txt">Lecture</a></div>',
                )
            else:
                yield SimpleNamespace(
                    url=url, headers={"Content-Type": "text/plain"}, data=b"The lecture explains MAC."
                )

        def read(self, response):
            return response.data

    sources, warnings = collect(Client(), course, a, tmp_path / "materials")
    assert not warnings
    assert len(sources) == 1
    assert sources[0]["text"] == "The lecture explains MAC."
    assert Path(sources[0]["file"]).exists()


def test_context_omissions_are_explicit(config):
    config.max_context_chars = 150
    ctx = build_context(config, {}, [{"id": "S1", "name": "Slides", "text": "x" * 1000}], [])
    assert not ctx["sources"]
    assert "omitted S1" in ctx["warnings"][0]


@pytest.mark.parametrize("fmt", ["tex", "docx", "md", "txt"])
def test_writes_real_artifact_and_never_overwrites(tmp_path, fmt):
    path = write_draft(tmp_path, answer(fmt))
    assert path.name == "main." + fmt
    if fmt == "docx":
        from docx import Document

        assert "A paragraph" in [p.text for p in Document(path).paragraphs]
    with pytest.raises(FileExistsError):
        write_draft(tmp_path, answer(fmt))


def test_codex_command_oauth_and_unknown_citation_rejected(config, monkeypatch):
    def run(command, **kwargs):
        assert "--ignore-user-config" in command
        assert 'forced_login_method="chatgpt"' in command
        assert "MOODLE_PASSWORD" not in kwargs["env"]
        output = Path(command[command.index("--output-last-message") + 1])
        payload = answer()
        payload["source_ids"] = ["fabricated"]
        output.write_text(json.dumps(payload))
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("MOODLE_PASSWORD", "test-secret")
    monkeypatch.setattr("moodle_automation.agent.subprocess.run", run)
    with pytest.raises(ValueError, match="unknown source"):
        draft(config, {"sources": [], "warnings": []})


def test_pipeline_idempotent_and_preserves_user_edits(config, monkeypatch):
    course, a = data()
    a["due_at"] = None
    calls = []
    monkeypatch.setattr("moodle_automation.cli.collect", lambda *args: ([], []))
    monkeypatch.setattr("moodle_automation.cli.compile_pdf", lambda source: source.with_suffix(".pdf"))

    def generate(*args):
        calls.append(True)
        return answer()

    monkeypatch.setattr("moodle_automation.cli.draft", generate)
    args = Namespace(assignment=None, include_submitted=False, include_overdue=False, regenerate=False)
    catalog = {"courses": [course]}
    first = process(config, None, catalog, args)
    target = Path(first[0]["directory"]) / "main.tex"
    target.write_text("USER EDIT")
    assert process(config, None, catalog, args)[0]["status"] == "unchanged"
    assert target.read_text() == "USER EDIT"
    args.regenerate = True
    second = process(config, None, catalog, args)
    assert second[0]["directory"] != first[0]["directory"]
    assert len(calls) == 2
    assert target.read_text() == "USER EDIT"


def test_failed_generation_retries(config, monkeypatch):
    course, a = data()
    a["due_at"] = None
    monkeypatch.setattr("moodle_automation.cli.collect", lambda *args: ([], []))
    monkeypatch.setattr("moodle_automation.cli.compile_pdf", lambda source: source.with_suffix(".pdf"))

    def fail(*args):
        raise RuntimeError("test failure")

    monkeypatch.setattr("moodle_automation.cli.draft", fail)
    args = Namespace(assignment=None, include_submitted=False, include_overdue=False, regenerate=False)
    catalog = {"courses": [course]}
    assert process(config, None, catalog, args)[0]["status"] == "failed"
    monkeypatch.setattr("moodle_automation.cli.draft", lambda *args: answer())
    assert process(config, None, catalog, args)[0]["status"] == "draft"


def test_submitted_and_unknown_not_generated(config):
    course, a = data()
    args = Namespace(assignment=None, include_submitted=False, include_overdue=False, regenerate=False)
    a["submitted"] = True
    assert process(config, None, {"courses": [course]}, args)[0]["status"] == "skipped_submitted"
    a["submitted"] = None
    assert process(config, None, {"courses": [course]}, args)[0]["status"] == "needs_review"


def test_filename_cannot_escape_directory():
    assert "/" not in safe_name("../../file.tex")
    assert "\\" not in safe_name("..\\file.tex")
