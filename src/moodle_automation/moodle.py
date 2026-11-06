import json
import time
from contextlib import contextmanager
from http.cookiejar import Cookie
from urllib.parse import urljoin, urlsplit

import requests

from .parsing import authenticated, links, parse_assignment, parse_course, soup
from .progress import stage, track
from .storage import write_json


class LoginRequired(RuntimeError):
    pass


class Moodle:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "MoodleDraftAutomation/0.1 (personal course reader)"
        self.auth_file = config.state_dir / "session.json"
        if not self.auth_file.exists():
            raise LoginRequired("Run: uv run moodle-auto login")
        state = json.loads(self.auth_file.read_text())
        for c in state.get("cookies", []):
            expires = c.get("expires", -1)
            if expires > 0 and expires < time.time():
                continue
            domain = c["domain"]
            host = urlsplit(config.base_url).hostname
            if not (host == domain.lstrip(".") or (domain.startswith(".") and host.endswith(domain))):
                continue
            self.session.cookies.set_cookie(
                Cookie(
                    version=0,
                    name=c["name"],
                    value=c["value"],
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=domain.startswith("."),
                    domain_initial_dot=domain.startswith("."),
                    path=c.get("path", "/"),
                    path_specified=True,
                    secure=c.get("secure", True),
                    expires=int(expires) if expires > 0 else None,
                    discard=expires <= 0,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": c.get("httpOnly", True)},
                    rfc2109=False,
                )
            )

    def allowed(self, url):
        a, b = urlsplit(url), urlsplit(self.config.base_url)
        return a.scheme == "https" and (a.hostname, a.port) == (b.hostname, b.port) and not a.username

    @contextmanager
    def get(self, url):
        # Validate every redirect before making the next request; never forward a session to another origin.
        response = None
        try:
            for _ in range(10):
                if not self.allowed(url):
                    raise RuntimeError("External URL requires manual review; only Moodle origin is fetched")
                response = self.session.get(url, timeout=(15, 60), allow_redirects=False, stream=True)
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers["Location"])
                    response.close()
                    if "/login/" in urlsplit(url).path or not self.allowed(url):
                        raise LoginRequired("Session expired or SSO redirect. Run: uv run moodle-auto login")
                    continue
                response.raise_for_status()
                if "/login/" in urlsplit(response.url).path:
                    raise LoginRequired("Moodle login required")
                yield response
                return
            raise RuntimeError("Too many Moodle redirects")
        finally:
            if response is not None:
                response.close()

    def read(self, response):
        limit = self.config.max_file_mb * 1024 * 1024
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > limit:
                raise RuntimeError(f"Download exceeded {self.config.max_file_mb} MiB")
        return bytes(data)

    def html(self, url):
        with self.get(url) as r:
            data = self.read(r).decode("utf-8", errors="replace")
        if soup(data).select_one('input[type="password"], body.notloggedin'):
            raise LoginRequired("Session expired. Run: uv run moodle-auto login")
        return data

    def check_login(self):
        html = self.html(self.config.base_url + "/")
        if not authenticated(html):
            raise LoginRequired("Could not verify Moodle login. Run: uv run moodle-auto login")
        return html

    def close(self):
        # Keep server-rotated Moodle cookies, but no unrelated SSO provider cookies.
        cookies = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "expires": c.expires or -1,
                "secure": c.secure,
                "httpOnly": True,
                "sameSite": "Lax",
            }
            for c in self.session.cookies
        ]
        write_json(self.auth_file, {"cookies": cookies, "origins": []})
        self.session.close()

    def scan(self):
        with stage("Moodleに接続", detail="認証状態・授業一覧を確認"):
            home = self.check_login()
        if self.config.course_ids:
            courses = [
                {"url": self.config.base_url + f"/course/view.php?id={i}"} for i in self.config.course_ids
            ]
        else:
            doc = soup(home)
            courses = links(
                doc.select_one("#frontpage-course-list") or doc, self.config.base_url, "/course/view.php"
            )
        if not courses:
            raise RuntimeError("No courses discovered. Set course_ids in moodle.toml")
        results, failures = [], []
        for entry in track(courses, "授業を確認", name=lambda c: c.get("name") or "授業ページを取得"):
            try:
                course = parse_course(self.html(entry["url"]), entry["url"])
                # Moodle's one-section-per-page layout may expose activities only on section pages.
                for section in track(course["sections"], "セクションを確認", name=lambda s: s["name"]):
                    part = parse_course(self.html(section["url"]), section["url"])
                    course["activities"].extend(part["activities"])
                course["activities"] = list({a["url"]: a for a in course["activities"]}.values())
                assignments = []
                for a in track(
                    [a for a in course["activities"] if a["kind"] == "assign"],
                    "課題の提出状況を確認",
                    name=lambda a: a["name"],
                ):
                    if a["kind"] == "assign":
                        try:
                            assignments.append(
                                parse_assignment(self.html(a["url"]), a, course, self.config.timezone)
                            )
                        except LoginRequired:
                            raise
                        except Exception as exc:
                            failures.append({"url": a["url"], "error": type(exc).__name__})
                course["assignments"] = assignments
                results.append(course)
            except LoginRequired:
                raise
            except Exception as exc:
                failures.append({"url": entry["url"], "error": type(exc).__name__})
        return {"courses": results, "failures": failures}


def login(config, timeout=600):
    from playwright.sync_api import sync_playwright

    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.state_dir.chmod(0o700)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(config.base_url + "/login/index.php")
        print("専用ブラウザでMoodleにログインしてください。ログイン完了を検出すると保存します。", flush=True)
        end = time.monotonic() + timeout
        try:
            while time.monotonic() < end:
                if page.is_closed():
                    raise LoginRequired("Browser closed before login completed")
                if (
                    urlsplit(page.url).netloc == urlsplit(config.base_url).netloc
                    and "/login/" not in page.url
                ):
                    if authenticated(page.content()):
                        state = context.storage_state()
                        # Persist only Moodle session cookies, not identity-provider cookies or local storage.
                        host = urlsplit(config.base_url).hostname
                        state["cookies"] = [
                            c
                            for c in state["cookies"]
                            if host == c["domain"].lstrip(".")
                            or (c["domain"].startswith(".") and host.endswith(c["domain"]))
                        ]
                        state["origins"] = []
                        write_json(config.state_dir / "session.json", state)
                        print("Moodleセッションを保存しました。")
                        return
                page.wait_for_timeout(1000)
            raise LoginRequired("Login timed out; run login again")
        finally:
            browser.close()
