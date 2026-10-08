"""Lấy một chương sangtacviet cho quy trình đọc.

Trang chương không nhét nội dung trong HTML. Trình duyệt phải chạy script của
site rồi mới điền #maincontent. Cách này giống crawl-lab: CloakBrowser hiện
cửa sổ, đọc chữ đã render, rồi đóng.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import urllib.request
from pathlib import Path

from .clean_subs import word_count
from .errors import PipelineError
from .layout import chunk_dir, translated_transcript

_CHAPTER = re.compile(
    r"https?://(?:www\.)?sangtacviet\.(?:com|vip|app)/truyen/([^/?#]+)/\d+/(\d+)/(\d+)/?",
    re.IGNORECASE,
)
_BOOK = re.compile(
    r"https?://(?:www\.)?sangtacviet\.(?:com|vip|app)/truyen/([^/?#]+)/\d+/(\d+)/?",
    re.IGNORECASE,
)
_NOTICE = "bạn đang đọc bản lưu"
_MAX_BATCH = 30
_HAN_STRONG = (
    "đạo hữu",
    "sư huynh",
    "sư muội",
    "sư đệ",
    "sư tỷ",
    "tiền bối",
    "hậu bối",
    "bổn tọa",
    "bổn vương",
    "tại hạ",
    "các hạ",
    "lão phu",
    "linh khí",
    "linh lực",
    "công tử",
    "thiếu gia",
    "cảnh giới",
    "tu vi",
    "y nói",
    "y cười",
    "y nhìn",
    "y liền",
    "y bèn",
    "hắn",
    "nàng",
    "ngươi",
    "gã",
)
_HAN_MILD = (
    "nhất thời",
    "thân thể",
    "không khỏi",
    "quả nhiên",
    "lạnh lùng",
    "khóe miệng",
    "khóe môi",
    "ánh mắt",
    "mở miệng",
)
_HAN_STRONG_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(item) for item in sorted(_HAN_STRONG, key=len, reverse=True)) + r")\b"
)
_HAN_MILD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(item) for item in sorted(_HAN_MILD, key=len, reverse=True)) + r")\b"
)


def parse_chapter_url(url: str) -> dict | None:
    match = _CHAPTER.search(url.strip())
    if not match:
        return None
    host, book, chapter = match.group(1), match.group(2), match.group(3)
    site = re.search(r"sangtacviet\.(?:com|vip|app)", url, re.IGNORECASE)
    domain = site.group(0).lower() if site else "sangtacviet.com"
    return {
        "id": chapter,
        "host": host,
        "book": book,
        "chapter": chapter,
        "url": f"https://{domain}/truyen/{host}/1/{book}/{chapter}/",
    }


def parse_book_url(url: str) -> dict | None:
    if parse_chapter_url(url):
        return None
    match = _BOOK.search(url.strip())
    if not match:
        return None
    host, book = match.group(1), match.group(2)
    site = re.search(r"sangtacviet\.(?:com|vip|app)", url, re.IGNORECASE)
    domain = site.group(0).lower() if site else "sangtacviet.com"
    return {
        "host": host,
        "book": book,
        "domain": domain,
        "url": f"https://{domain}/truyen/{host}/1/{book}/",
    }


def _chapter_number(title: str) -> int | None:
    match = re.search(r"thứ\s*([\d][\d.,]*)\s*(?:chương|chap|hồi)", title, re.IGNORECASE)
    if not match:
        match = re.search(r"(?:chương|chap|hồi)\s*([\d][\d.,]*)", title, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1).replace(".", "").replace(",", ""))


def list_chapters(url: str) -> list[dict]:
    """Đọc danh sách chương bằng API của site, không mở trình duyệt."""
    book = parse_book_url(url)
    if book is None:
        parsed = parse_chapter_url(url)
        if parsed is None:
            raise PipelineError("Link sangtacviet không đúng dạng trang truyện hoặc trang chương")
        site = re.search(r"sangtacviet\.(?:com|vip|app)", url, re.IGNORECASE)
        book = {
            "host": parsed["host"],
            "book": parsed["book"],
            "domain": site.group(0).lower() if site else "sangtacviet.com",
        }
    endpoint = (
        f"https://{book['domain']}/index.php?ngmar=chapterlist"
        f"&h={book['host']}&bookid={book['book']}&sajax=getchapterlist"
    )
    request = urllib.request.Request(
        endpoint,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://{book['domain']}/truyen/{book['host']}/1/{book['book']}/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise PipelineError(f"Không lấy được danh sách chương: {exc}") from exc
    raw = payload.get("data") if isinstance(payload, dict) else ""
    if not raw:
        raise PipelineError("Trang truyện không có danh sách chương")
    rows: list[dict] = []
    extras: list[dict] = []
    for index, piece in enumerate(str(raw).split("-//-")):
        cols = piece.split("-/-")
        if len(cols) < 3 or not cols[1].strip():
            continue
        title = cols[2].strip()
        chapter = cols[1].strip()
        item = {
            "index": index,
            "id": chapter,
            "title": title,
            "url": f"https://{book['domain']}/truyen/{book['host']}/{cols[0].strip() or '1'}/{book['book']}/{chapter}/",
        }
        number = _chapter_number(title)
        if number is None:
            extras.append(item)
            continue
        item["number"] = number
        rows.append(item)
    if not rows:
        for offset, item in enumerate(extras, start=1):
            item["number"] = offset
            rows.append(item)
    rows.sort(key=lambda row: (row["number"], row["index"]))
    if not rows:
        raise PipelineError("Trang truyện không có danh sách chương")
    return rows


def chapters_in_range(url: str, start: int, end: int) -> list[dict]:
    if start < 1 or end < 1:
        raise PipelineError("Số chương phải từ 1")
    if start > end:
        start, end = end, start
    chosen = [row for row in list_chapters(url) if start <= row["number"] <= end]
    if not chosen:
        raise PipelineError(f"Không thấy chương từ {start} đến {end}")
    if len(chosen) > _MAX_BATCH:
        raise PipelineError(f"Mỗi lần tối đa {_MAX_BATCH} chương. Chọn khoảng hẹp hơn.")
    return chosen


def chapter_meta(url: str, title: str | None = None) -> dict:
    parsed = parse_chapter_url(url)
    if parsed is None:
        raise ValueError("Link chương sangtacviet không đúng dạng /truyen/<nguồn>/1/<truyện>/<chương>/")
    return {
        "id": parsed["id"],
        "title": title or "Chương Sangtacviet",
        "chapterTitle": title or "",
        "channel": "sangtacviet",
        "url": parsed["url"],
        "source": "sangtacviet",
        "bookId": parsed["book"],
        "host": parsed["host"],
    }


def _clean(raw: str, title: str) -> str:
    kept: list[str] = []
    blank = False
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("@") or _NOTICE in text.lower():
            if kept and not blank:
                kept.append("")
                blank = True
            continue
        kept.append(text)
        blank = False
    body = "\n".join(kept).strip()
    if title and body.startswith(title):
        body = body[len(title) :].strip()
    if title:
        return f"{title}\n\n{body}\n"
    return body + "\n"


async def _read_page(url: str) -> tuple[str, str, str]:
    from cloakbrowser import launch_context_async

    ctx = await launch_context_async(headless=False, humanize=True, locale="vi-VN")
    try:
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            await page.wait_for_function(
                "() => window.chapterfetcher && window.chapterfetcher.readyState >= 1",
                timeout=20_000,
            )
        except Exception:
            pass
        await page.evaluate(
            """() => {
                if (window.chapterfetcher && window.chapterfetcher.readyState === 1) {
                    window.chapterfetcher.send('');
                }
            }"""
        )
        info = {"text": "", "title": "", "book": ""}
        for _ in range(40):
            info = await page.evaluate(
                """() => {
                    const box = document.querySelector('#maincontent') || document.querySelector('[id^="cld-"]');
                    const name = document.querySelector('#bookchapnameholder');
                    const book = document.querySelector('#booknameholder');
                    return {
                      text: box ? box.innerText : '',
                      title: name ? name.textContent.trim() : '',
                      book: book ? book.textContent.trim() : ''
                    };
                }"""
            )
            text = (info.get("text") or "").strip()
            title = (info.get("title") or "").strip()
            if len(text) > 400 and title and title != "_":
                return text, title, (info.get("book") or "").strip()
            await asyncio.sleep(0.75)
        preview = " ".join((info.get("text") or "").split())[:180]
        raise PipelineError(preview or "Trang chương không hiện nội dung")
    finally:
        await ctx.close()


_NATURAL = """Bạn sửa bản chữ đang quá Hán Việt thành tiếng Việt nói tự nhiên, để đọc thành tiếng dễ nghe.
Giữ đủ ý, đúng thứ tự, đúng tên người, tên chiêu, địa danh, số liệu và lời thoại. Không tóm tắt, không chú thích, không thêm câu.
Đổi cách diễn Hán Việt cứng sang câu tiếng Việt thông dụng. Ngôi xưng trong thoại phải hợp vai.
Tên riêng và thuật ngữ tu luyện đã quen thì giữ nguyên.
Nếu bản gốc thiếu dấu câu thì thêm dấu câu cho dễ nghe. Bản gốc đã chia đoạn thì giữ cách chia đó.
Chỉ trả về bản đã sửa.

Bảng thuật ngữ:
{glossary}"""


def _raw_path(job_dir: Path) -> Path:
    return job_dir / "vi" / "raw.txt"


def natural_mark(job_dir: Path) -> Path:
    return job_dir / "vi" / "natural.ok"


def looks_han_viet(text: str) -> bool:
    """Phụ đề tiếng Việt của video tu tiên / dịch truyện thường đặc hơn lời nói thường."""
    sample = text.lower()
    words = word_count(sample)
    if words < 60:
        return False
    strong = len(_HAN_STRONG_RE.findall(sample))
    if strong < 3:
        return False
    mild = len(_HAN_MILD_RE.findall(sample))
    return (strong * 3 + mild) * 1000 / words >= 10


def _save_book_title(job_dir: Path, title: str, book: str) -> None:
    meta_path = job_dir / "meta.json"
    if not meta_path.exists():
        return
    import json

    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    label = f"{book} — {title}" if book else title
    saved["title"] = label
    saved["book"] = book
    saved["chapterTitle"] = title
    meta_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_raw(url: str, job_dir: Path, *, force: bool, on_progress=None) -> str:
    raw_path = _raw_path(job_dir)
    out = translated_transcript(job_dir)
    if not force and raw_path.exists() and raw_path.stat().st_size > 200:
        return raw_path.read_text(encoding="utf-8")
    if not force and out.exists() and out.stat().st_size > 200 and not natural_mark(job_dir).exists():
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        return raw_path.read_text(encoding="utf-8")

    meta = chapter_meta(url)
    if on_progress:
        on_progress("Đang mở trình duyệt để lấy chương...")
    try:
        raw, title, book = asyncio.run(_read_page(meta["url"]))
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Không lấy được chương: {exc}") from exc

    text = _clean(raw, title)
    if word_count(text) < 40:
        raise PipelineError("Chương lấy về quá ngắn")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(text, encoding="utf-8")
    _save_book_title(job_dir, title, book)
    if on_progress:
        on_progress(f"Đã lấy chương: {word_count(text):,} từ.")
    return text


def _rewrite_piece(translate, system: str, chunk: str, prev: str, on_progress=None) -> str:
    from .translate import call_with_retry

    if prev:
        user = (
            "Đoạn gốc liền trước (chỉ để giữ ngôi xưng, KHÔNG viết lại):\n"
            f"{prev}\n\n---\nViết lại đoạn sau cho dễ đọc:\n{chunk}"
        )
    else:
        user = f"Viết lại đoạn sau cho dễ đọc:\n{chunk}"
    text, truncated = call_with_retry(translate, system, user, 8192, on_progress=on_progress)
    words = chunk.split()
    if truncated and len(words) > 280:
        mid = len(words) // 2
        left = _rewrite_piece(translate, system, " ".join(words[:mid]), prev, on_progress)
        right = _rewrite_piece(translate, system, " ".join(words[mid:]), left[-400:], on_progress)
        return f"{left}\n\n{right}".strip()
    if not text:
        raise PipelineError("API trả về rỗng. Chạy lại để làm tiếp.")
    return text


def naturalize_chapter(job_dir: Path, raw: str, *, force: bool = False, on_progress=None) -> str:
    """Viết lại bản Hán Việt thành tiếng Việt đọc được. Các đoạn chạy song song."""
    from concurrent.futures import ThreadPoolExecutor

    from .translate import Translator, load_glossary, load_keys, merge_files, paragraphs_to_chunks, report

    out = translated_transcript(job_dir)
    mark = natural_mark(job_dir)
    if not force and mark.exists() and out.exists() and out.stat().st_size > 200:
        text = out.read_text(encoding="utf-8")
        if on_progress:
            on_progress(f"Đã có bản đọc: {word_count(text):,} từ.")
        return text

    chunks = paragraphs_to_chunks(raw, 1600)
    out_dir = chunk_dir(job_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if force:
        for path in out_dir.glob("*.txt"):
            path.unlink()
        if mark.exists():
            mark.unlink()

    pending = [n for n, _chunk in enumerate(chunks) if not (out_dir / f"{n:04d}.txt").exists()]
    total = len(chunks)
    if pending:
        keys = load_keys()
        system = _NATURAL.format(glossary=load_glossary(job_dir / "glossary.txt"))
        workers = min(4, len(pending))
        translators = [Translator("gemini", "gemini-3.1-flash-lite", keys) for _ in range(workers)]
        locks = [threading.Lock() for _ in translators]
        report(on_progress, f"Đang chuyển Hán Việt, {len(pending)}/{total} đoạn cùng lúc", total - len(pending), total)

        prevs = []
        for n in pending:
            prev = ""
            if n:
                tail = chunks[n - 1].strip()
                prev = tail[-450:]
            prevs.append(prev)

        def work(slot: int, index: int, chunk: str, prev: str) -> None:
            with locks[slot]:
                rewritten = _rewrite_piece(translators[slot], system, chunk, prev, on_progress)
            (out_dir / f"{index:04d}.txt").write_text(rewritten.strip() + "\n", encoding="utf-8")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(work, i % workers, index, chunks[index], prev)
                for i, (index, prev) in enumerate(zip(pending, prevs))
            ]
            done = total - len(pending)
            for future in futures:
                future.result()
                done += 1
                report(on_progress, f"Đã chuyển {done}/{total}", done, total)

    merge_files(out_dir, out, quiet=True)
    mark.write_text("1", encoding="utf-8")
    text = out.read_text(encoding="utf-8")
    if on_progress:
        on_progress(f"Đã chuyển sang tiếng Việt đọc được: {word_count(text):,} từ.")
    return text


def fetch_chapter(url: str, job_dir: Path, *, force: bool = False, on_progress=None) -> str:
    raw = _load_raw(url, job_dir, force=force, on_progress=on_progress)
    return naturalize_chapter(job_dir, raw, force=force, on_progress=on_progress)
