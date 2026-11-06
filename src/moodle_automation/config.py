import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values


@dataclass
class Config:
    base_url: str
    state_dir: Path
    output_dir: Path
    course_ids: tuple[int, ...] = ()
    timezone: str = "Asia/Tokyo"
    language: str = "日本語"
    max_file_mb: int = 50
    max_context_chars: int = 180000
    codex_timeout_seconds: int = 900
    model: str = ""

    @classmethod
    def load(cls, path: Path):
        path = path.resolve()
        # Only import the two supported identity fields; shell values take precedence.
        values = dotenv_values(path.parent / ".env", interpolate=False)
        for key in ("MOODLE_STUDENT_ID", "MOODLE_STUDENT_NAME"):
            if values.get(key) is not None:
                os.environ.setdefault(key, values[key])
        data = tomllib.loads(path.read_text())
        data["base_url"] = data["base_url"].rstrip("/")
        url = urlsplit(data["base_url"])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("base_url must be an HTTPS Moodle URL without credentials/query/fragment")
        for key, default in (("state_dir", ".moodle"), ("output_dir", "output")):
            data[key] = (path.parent / data.get(key, default)).resolve()
        data["course_ids"] = tuple(int(i) for i in data.get("course_ids", []))
        obj = cls(**data)
        ZoneInfo(obj.timezone)
        if min(obj.max_file_mb, obj.max_context_chars, obj.codex_timeout_seconds) <= 0:
            raise ValueError("Size and timeout limits must be positive")
        return obj
