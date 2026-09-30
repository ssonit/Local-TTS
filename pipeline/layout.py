"""Bố cục một job trên đĩa.

jobs/<tên>_<video_id>/
  status.json
  meta.json
  glossary.txt
  en/transcript.txt
  en/subs/
  en/parts/
  vi/transcript.txt
  vi/chunks/
  vi/parts/
  asr/
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JOBS = REPO / "jobs"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def check_video_id(video_id: str) -> str:
    if not _ID.fullmatch(video_id):
        raise ValueError("Id video không hợp lệ")
    return video_id


def slugify(title: str) -> str:
    text = title.strip().lower()
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-+", "-", text).strip("-. ")
    return text[:48]


def folder_name(video_id: str, title: str | None) -> str:
    check_video_id(video_id)
    slug = slugify(title or "")
    if not slug or slug == video_id.lower():
        return video_id
    return f"{slug}_{video_id}"


def find_job_dir(video_id: str) -> Path | None:
    check_video_id(video_id)
    if not JOBS.exists():
        return None
    matches = [
        path
        for path in JOBS.iterdir()
        if path.is_dir() and (path.name == video_id or path.name.endswith(f"_{video_id}"))
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"Có hơn một thư mục cho video {video_id}")
    found = matches[0].resolve()
    if found.parent != JOBS.resolve():
        raise ValueError("Id video không hợp lệ")
    return found


def ensure_job_dir(video_id: str, title: str | None = None) -> Path:
    found = find_job_dir(video_id)
    if found:
        return found
    JOBS.mkdir(exist_ok=True)
    path = JOBS / folder_name(video_id, title)
    path.mkdir(exist_ok=True)
    return path.resolve()


def source_transcript(root: Path) -> Path:
    nested = root / "en" / "transcript.txt"
    legacy = root / "transcript.txt"
    if nested.exists() or not legacy.exists():
        return nested
    return legacy


def chunk_dir(root: Path) -> Path:
    nested = root / "vi" / "chunks"
    legacy = root / "chunks_vi"
    if nested.exists() or not legacy.exists():
        return nested
    return legacy


def translated_transcript(root: Path) -> Path:
    nested = root / "vi" / "transcript.txt"
    legacy = root / "transcript_vi.txt"
    if nested.exists():
        return nested
    if legacy.exists() and not (root / "vi" / "chunks").exists():
        return legacy
    return nested
