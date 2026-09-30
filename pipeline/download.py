"""Tải phụ đề tiếng Anh gốc rồi làm sạch, ghi transcript.txt trong thư mục job.

Dịch từ bản Anh đã làm sạch. Phụ đề tiếng Việt sẵn của YouTube là Google Dịch,
câu cứng và vẫn lặp dòng.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .clean_subs import clean_cues, load_cues, word_count
from .errors import NoCaptionsError
from .layout import source_transcript

DEFAULT_URL = "https://youtu.be/Ok5iPIjEsyQ"


def report(on_progress, message: str, done: int | None = None, total: int | None = None) -> None:
    print(message, flush=True)
    if on_progress:
        on_progress(message, done, total)


def video_id(url: str) -> str:
    value = url.strip()
    match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    raise ValueError(f"Không tách được id từ link: {url}")


def fetch_meta(url: str) -> dict:
    vid = video_id(url)
    meta = {"id": vid, "title": vid, "channel": "", "url": f"https://youtu.be/{vid}"}
    endpoint = f"https://www.youtube.com/oembed?url=https://youtu.be/{vid}&format=json"
    try:
        req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.load(response)
        meta["title"] = data.get("title") or vid
        meta["channel"] = data.get("author_name") or ""
    except Exception:
        pass
    return meta


def language_from_listing(text: str) -> str:
    """Đọc kết quả yt-dlp --list-subs. Phụ đề gốc ưu tiên hơn bản Google Dịch."""
    manual: set[str] = set()
    auto: set[str] = set()
    orig: set[str] = set()
    bucket: set[str] | None = None
    for line in text.splitlines():
        low = line.lower()
        if "automatic caption" in low:
            bucket = auto
            continue
        if "available subtitles" in low:
            bucket = manual
            continue
        if bucket is None or not line.strip() or line.lower().startswith("language"):
            continue
        token = line.split()[0]
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)?", token):
            continue
        base = token.split("-")[0].lower()
        if token.lower().endswith("-orig"):
            orig.add(base)
        else:
            bucket.add(base)
    if "vi" in orig or "vi" in manual:
        return "vi"
    if "en" in orig or "en" in manual:
        return "en"
    if "vi" in auto and "en" not in auto and "en" not in orig:
        return "vi"
    if "en" in auto or "en" in orig:
        return "en"
    return "unknown"


def probe_language(url: str) -> str:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-update",
        "--skip-download",
        "--list-subs",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    listing = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 and "available" not in listing.lower():
        return "unknown"
    return language_from_listing(listing)


def _rank(path: Path, lang: str = "en") -> tuple[int, int]:
    name = path.name.lower()
    if f".{lang}-orig." in name:
        rank = 0
    elif f".{lang}." in name:
        rank = 1
    else:
        rank = 2
    if name.endswith(".json3"):
        fmt = 0
    elif name.endswith(".srt"):
        fmt = 1
    else:
        fmt = 2
    return (rank, fmt)


def _ytdlp(url: str, subs_dir: Path, cookies: str | None, sub_langs: str) -> None:
    subs_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-update",
        "--skip-download",
        "--write-auto-subs",
        "--sub-langs",
        sub_langs,
        "--sub-format",
        "json3/srt/vtt/best",
        "-o",
        str(subs_dir / "%(id)s"),
        url,
    ]
    if cookies:
        cmd[3:3] = ["--cookies-from-browser", cookies]
    last = ""
    for attempt in range(4):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        last = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return
        if "429" in last or "Too Many Requests" in last:
            wait = 20 * (attempt + 1)
            print(f"YouTube 429, đợi {wait}s rồi thử lại...", flush=True)
            time.sleep(wait)
            continue
        break
    print(last[-1500:], flush=True)
    raise RuntimeError("yt-dlp không tải được phụ đề")


def _via_api(vid: str, languages: tuple[str, ...]) -> list[str]:
    from youtube_transcript_api import YouTubeTranscriptApi

    fetched = YouTubeTranscriptApi().fetch(vid, languages=languages)
    return [snip.text for snip in fetched]


def _pick_sub(subs_dir: Path, vid: str, lang: str = "en") -> Path | None:
    files = [
        p
        for p in subs_dir.glob(f"{vid}.*")
        if p.suffix.lower() in {".json3", ".srt", ".vtt"} or p.name.lower().endswith(".json3")
    ]
    if not files:
        return None
    return sorted(files, key=lambda path: _rank(path, lang))[0]


def fetch_captions(
    url: str,
    job_dir: Path,
    *,
    lang: str = "en",
    force: bool = False,
    cookies: str | None = None,
    on_progress=None,
    output_name: str = "transcript.txt",
    grouped: bool = False,
) -> str:
    """Tải phụ đề gốc của một ngôn ngữ. Tiếng Việt ghi vào vi/, tiếng Anh ghi vào en/."""
    job_dir.mkdir(parents=True, exist_ok=True)
    label = "tiếng Việt" if lang == "vi" else "tiếng Anh"
    sub_langs = "vi-orig,vi" if lang == "vi" else "en-orig,en"
    if grouped:
        subs_dir = job_dir / lang / "subs"
        out = job_dir / "vi" / "transcript.txt" if lang == "vi" else source_transcript(job_dir)
    else:
        subs_dir = job_dir / "subs"
        out = job_dir / output_name
    if out.exists() and out.stat().st_size > 0 and not force:
        text = out.read_text(encoding="utf-8")
        report(on_progress, f"Đã có phụ đề {label}: {word_count(text):,} từ.")
        return text

    vid = video_id(url)
    report(on_progress, f"Đang tải phụ đề {label}...")
    cues: list[str] = []
    failures: list[str] = []
    try:
        _ytdlp(url, subs_dir, cookies, sub_langs)
        picked = _pick_sub(subs_dir, vid, lang)
        if picked is None:
            raise RuntimeError("yt-dlp chạy xong nhưng không thấy file phụ đề")
        report(on_progress, f"Dùng file: {picked.name}")
        cues = load_cues(picked)
    except Exception as exc:
        failures.append(str(exc))
        report(on_progress, "yt-dlp không lấy được phụ đề, thử nguồn khác...")
        try:
            cues = _via_api(vid, ("vi",) if lang == "vi" else ("en",))
        except Exception as api_exc:
            failures.append(str(api_exc))
            raise NoCaptionsError(f"Không có phụ đề {label}. " + " | ".join(failures)) from api_exc

    if not cues:
        raise NoCaptionsError(f"Không có phụ đề {label}.")

    report(on_progress, "Đang làm sạch phụ đề...")
    raw_words = word_count(" ".join(cues))
    text = clean_cues(cues)
    cleaned = word_count(text)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    ratio = (raw_words / cleaned) if cleaned else 0
    report(
        on_progress,
        f"Phụ đề sạch: {cleaned:,} từ (thô {raw_words:,} từ, gấp khoảng {ratio:.1f} lần).",
    )
    return text


def fetch_english_transcript(
    url: str,
    job_dir: Path,
    *,
    force: bool = False,
    cookies: str | None = None,
    on_progress=None,
    output_name: str = "transcript.txt",
    grouped: bool = False,
) -> str:
    return fetch_captions(
        url,
        job_dir,
        lang="en",
        force=force,
        cookies=cookies,
        on_progress=on_progress,
        output_name=output_name,
        grouped=grouped,
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Tải và làm sạch phụ đề tiếng Anh")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--force", action="store_true", help="tải lại dù đã có transcript.txt")
    parser.add_argument("--cookies-from-browser", default=None, help="vd: edge hoặc chrome")
    parser.add_argument("-o", "--output", default="transcript.txt")
    args = parser.parse_args()

    out = Path(args.output)
    try:
        fetch_english_transcript(
            args.url,
            out.parent,
            force=args.force,
            cookies=args.cookies_from_browser,
            output_name=out.name,
        )
    except (ValueError, RuntimeError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
