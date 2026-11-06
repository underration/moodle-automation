import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


def soup(html):
    return BeautifulSoup(html, "html.parser")


def text(node):
    return node.get_text(" ", strip=True) if node else ""


def canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def moodle_id(url):
    return int(parse_qs(urlsplit(url).query)["id"][0])


def links(node, base, kind=None):
    result = {}
    for a in node.select("a[href]"):
        url = canonical(urljoin(base, a["href"]))
        if kind and not urlsplit(url).path.endswith(kind):
            continue
        result.setdefault(url, {"url": url, "name": text(a)})
    return list(result.values())


def authenticated(html):
    doc = soup(html)
    return bool(doc.select_one('a[href*="/login/logout.php"], #user-menu-toggle, .usermenu')) and not bool(
        doc.select_one('input[type="password"], body.notloggedin')
    )


def parse_course(html, url):
    doc = soup(html)
    main = doc.select_one("#region-main") or doc
    activities = []
    for a in main.select('a[href*="/mod/"]'):
        target = canonical(urljoin(url, a["href"]))
        match = re.search(r"/mod/(assign|resource|folder|page|book|url)/view\.php$", urlsplit(target).path)
        if not match:
            continue
        section = a.find_parent("li", class_="section") or a.find_parent(attrs={"data-for": "section"})
        section_name = text(section.select_one("h3, .sectionname")) if section else ""
        activities.append({"url": target, "name": text(a), "kind": match[1], "section": section_name})
    return {
        "id": moodle_id(url),
        "name": text(doc.select_one("h1")),
        "url": url,
        "activities": list({a["url"]: a for a in activities}.values()),
        "sections": links(main, url, "/course/section.php"),
    }


def parse_due(raw, timezone):
    # Retain raw text for unsupported locales rather than guessing a deadline.
    match = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日.*?(\d{1,2}):(\d{2})", raw)
    if match:
        return datetime(*map(int, match.groups()), tzinfo=ZoneInfo(timezone)).isoformat()
    return None


def parse_assignment(html, activity, course, timezone):
    doc = soup(html)
    intro = doc.select_one("#intro")
    # Some themes place the intro in the activity description instead.
    if intro is None:
        intro = doc.select_one('[data-region="activity-description"]')
    status_node = doc.select_one('.submissionstatustable [class*="submissionstatus"]')
    if status_node is None:
        for row in doc.select(".submissionstatustable tr"):
            if text(row.select_one("th")).rstrip(":：").lower() in ("提出ステータス", "submission status"):
                status_node = row.select_one("td")
                break
    status = text(status_node)
    classes = status_node.get("class", []) if status_node else []
    if "submissionstatussubmitted" in classes:
        submitted = True
    elif any(
        c in classes for c in ("submissionstatusnew", "submissionstatusdraft", "submissionstatusnosubmission")
    ):
        submitted = False
    elif re.search(r"まだ提出|未提出|下書き|no (?:attempt|submission)|not submitted|draft", status, re.I):
        submitted = False
    elif re.search(r"提出済み|submitted for grading", status, re.I):
        submitted = True
    else:
        submitted = None
    due_raw = ""
    for row in doc.select('[data-region="activity-dates"] .date-item, .submissionstatustable tr'):
        label = text(row.select_one(".date-item-label, th"))
        if re.search(r"期限|締切|due date|^due:", label, re.I):
            due_raw = text(row)
    return {
        "id": moodle_id(activity["url"]),
        "course_id": course["id"],
        "course_name": course["name"],
        "name": text(doc.select_one("h1")) or activity["name"],
        "url": activity["url"],
        "section": activity["section"],
        "description": text(intro),
        "attachments": links(intro, activity["url"]) if intro else [],
        "submitted": submitted,
        "submission_status": status or "unknown",
        "due_raw": due_raw,
        "due_at": parse_due(due_raw, timezone),
    }
