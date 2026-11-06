import hashlib
import re
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from .moodle import LoginRequired
from .parsing import links, soup, text
from .progress import detail, drain, track


def safe_name(name):
    name = re.sub(r"[^\w.\-()一-龥ぁ-んァ-ヶ ]", "_", Path(name.replace("\\", "/")).name)
    return name[:160].lstrip(".") or "material"


def extract(path):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        pages = [f"[page {i}]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages, 1)]
        if not any((p.extract_text() or "").strip() for p in reader.pages):
            raise ValueError("Scanned PDF: OCR or manual inspection required")
        return "\n\n".join(pages)
    if suffix == ".docx":
        from docx import Document

        doc = Document(path)
        return "\n".join(
            [p.text for p in doc.paragraphs]
            + [" | ".join(c.text for c in r.cells) for t in doc.tables for r in t.rows]
        )
    if suffix == ".pptx":
        from pptx import Presentation

        return "\n\n".join(
            f"[slide {i}]\n" + "\n".join(s.text for s in slide.shapes if s.has_text_frame)
            for i, slide in enumerate(Presentation(path).slides, 1)
        )
    if suffix in (".txt", ".md", ".tex", ".csv"):
        return path.read_text(encoding="utf-8-sig")
    raise ValueError(f"Unsupported extraction format: {suffix}; inspect original file")


def collect(client, course, assignment, directory):
    directory.mkdir(parents=True, exist_ok=True)
    sources, warnings, visited = [], [], set()
    queue = [dict(x, section=assignment["section"]) for x in assignment["attachments"]]
    materials = [a for a in course["activities"] if a["kind"] != "assign"]
    # Same-section content goes first; retain other lectures for definitions and earlier prerequisites.
    materials.sort(key=lambda a: a["section"] != assignment["section"])
    queue.extend(materials)
    for item in track(drain(queue), "資料を取得", name=lambda item: item["name"]):
        url = item["url"]
        if url in visited:
            continue
        visited.add(url)
        if len(visited) > 300:
            warnings.append("Material traversal reached 300 URLs; remaining links require review")
            break
        if not client.allowed(url) or item.get("kind") == "url":
            warnings.append(f"External/video link not downloaded: {item['name']} ({url})")
            continue
        try:
            with client.get(url) as response:
                data = client.read(response)
                content_type = response.headers.get("Content-Type", "").lower()
                final_url = response.url
                disposition = response.headers.get("Content-Disposition", "")
            if "text/html" in content_type or data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                doc = soup(data.decode("utf-8", errors="replace"))
                if doc.select_one('input[type="password"], body.notloggedin'):
                    raise LoginRequired("Moodle session expired while downloading materials")
                if doc.select_one('.errorcode, [data-rel="fatalerror"]'):
                    raise ValueError("Moodle resource error page")
                main = doc.select_one("#region-main") or doc
                # Only follow resource content links, never forms, submissions, logout, or profile links.
                children = links(main, final_url)
                for embed in main.select("object[data], iframe[src], embed[src]"):
                    children.append(
                        {
                            "url": urljoin(final_url, embed.get("data") or embed.get("src")),
                            "name": item["name"],
                        }
                    )
                followed = 0
                for child in children:
                    path = urlsplit(child["url"]).path
                    if (
                        "/pluginfile.php/" in path
                        and "/assignsubmission_" not in path
                        or "/mod/book/view.php" in path
                    ):
                        queue.append(dict(child, section=item.get("section", "")))
                        followed += 1
                body = main.select_one(".generalbox, .book_content, .resourcecontent, .mod_introbox")
                if body and text(body):
                    sources.append(
                        {
                            "id": "S" + str(len(sources) + 1),
                            "name": item["name"],
                            "url": url,
                            "section": item.get("section", ""),
                            "text": text(body),
                            "file": None,
                        }
                    )
                elif not followed:
                    warnings.append(f"No downloadable/text material found: {item['name']} ({url})")
                continue
            msg = Message()
            msg["content-disposition"] = disposition
            filename = msg.get_filename() or unquote(urlsplit(final_url).path.rsplit("/", 1)[-1])
            if not Path(filename).suffix or filename.endswith(".php"):
                ext = {"application/pdf": ".pdf", "text/plain": ".txt"}.get(
                    content_type.split(";")[0], ".bin"
                )
                filename = item["name"] + ext
            digest = hashlib.sha256(data).hexdigest()
            path = directory / f"{digest[:12]}-{safe_name(filename)}"
            path.write_bytes(data)
            try:
                detail(f"文字を抽出: {path.name}（{len(data) / 1024 / 1024:.1f} MiB）")
                content = extract(path)
                if not content.strip():
                    raise ValueError("Empty extracted text")
            except Exception as exc:
                warnings.append(f"Text extraction requires review: {path.name} ({type(exc).__name__})")
                content = "[Extraction unavailable; original file must be reviewed]"
            sources.append(
                {
                    "id": "S" + str(len(sources) + 1),
                    "name": item["name"],
                    "url": url,
                    "section": item.get("section", ""),
                    "text": content,
                    "file": str(path.resolve()),
                    "sha256": digest,
                }
            )
        except LoginRequired:
            raise
        except Exception as exc:
            warnings.append(f"Download failed: {item['name']} ({type(exc).__name__})")
    return sources, warnings
