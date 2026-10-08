"""Máy chủ job local cho quy trình YouTube -> bản dịch.

Chỉ nghe 127.0.0.1. Phụ đề, bản dịch và trạng thái nằm trong jobs/ trên đĩa.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .download import fetch_captions, fetch_english_transcript, fetch_meta, probe_language, video_id
from .sangtacviet import (
    chapter_meta,
    chapters_in_range,
    fetch_chapter,
    looks_han_viet,
    natural_mark,
    naturalize_chapter,
    parse_book_url,
    parse_chapter_url,
)
from .errors import NoCaptionsError, PipelineError
from .layout import JOBS, chunk_dir, ensure_job_dir, find_job_dir, translated_transcript
from .transcribe import run_transcribe
from .translate import run_translation

HOST = "127.0.0.1"
PORT = int(os.environ.get("PIPELINE_PORT", "8765"))

_lock = threading.Lock()
_queue: queue.Queue[str] = queue.Queue()


def _job_dir(job_id: str) -> Path:
    found = find_job_dir(job_id)
    if found is None:
        raise FileNotFoundError("Không thấy job")
    return found


def _write_status(job_id: str, **patch) -> dict:
    directory = _job_dir(job_id)
    path = directory / "status.json"
    with _lock:
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data.update(patch)
        data["id"] = job_id
        data["updated"] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return data


def _read_status(job_id: str) -> dict:
    path = _job_dir(job_id) / "status.json"
    if not path.exists():
        raise FileNotFoundError("Không thấy job")
    with _lock:
        return json.loads(path.read_text(encoding="utf-8"))


def _split_listen(text: str, target: int = 1500) -> list[str]:
    paras = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    parts: list[str] = []
    buf: list[str] = []
    count = 0

    def flush() -> None:
        nonlocal count
        if buf:
            parts.append("\n\n".join(buf))
            buf.clear()
            count = 0

    for para in paras:
        words = len(para.split())
        if count and count + words > target:
            flush()
        buf.append(para)
        count += words
    flush()
    return parts


def _listen_dir(directory: Path) -> Path:
    return directory / "vi" / "listen"


def _ensure_listen_parts(directory: Path) -> list[Path]:
    """Chia bản dịch thành từng phần khoảng 1500 từ để tạo audio."""
    transcript = translated_transcript(directory)
    listen = _listen_dir(directory)
    stamp = directory / "vi" / "listen.stamp"
    if not transcript.exists() or transcript.stat().st_size == 0:
        return []
    marker = str(transcript.stat().st_size)
    files = sorted(listen.glob("*.txt")) if listen.exists() else []
    if stamp.exists() and stamp.read_text(encoding="utf-8") == marker and files:
        return files
    if listen.exists():
        shutil.rmtree(listen)
    listen.mkdir(parents=True)
    parts = _split_listen(transcript.read_text(encoding="utf-8"))
    written: list[Path] = []
    for index, part in enumerate(parts, start=1):
        path = listen / f"{index:04d}.txt"
        path.write_text(part.strip() + "\n", encoding="utf-8")
        written.append(path)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(marker, encoding="utf-8")
    return written


def _public(job_id: str, *, with_text: bool = False, part: int = 1, full: bool = False) -> dict:
    data = _read_status(job_id)
    data.pop("force", None)
    if with_text:
        directory = _job_dir(job_id)
        glossary = directory / "glossary.txt"
        data["glossary"] = glossary.read_text(encoding="utf-8") if glossary.exists() else ""
        if data.get("status") in {"queued", "running"}:
            data["text"] = ""
            data["part"] = 1
            data["partCount"] = 0
            return data
        if full:
            vi = translated_transcript(directory)
            data["text"] = vi.read_text(encoding="utf-8") if vi.exists() else ""
            data["part"] = 1
            data["partCount"] = len(_ensure_listen_parts(directory))
            return data
        files = _ensure_listen_parts(directory)
        count = len(files)
        chosen = min(max(part, 1), count) if count else 1
        data["part"] = chosen
        data["partCount"] = count
        data["text"] = files[chosen - 1].read_text(encoding="utf-8") if count else ""
    return data


def _list_jobs() -> list[dict]:
    if not JOBS.exists():
        return []
    items = []
    for directory in JOBS.iterdir():
        status = directory / "status.json"
        if not directory.is_dir() or not status.exists():
            continue
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        data.pop("force", None)
        items.append(data)
    items.sort(key=lambda item: item.get("updated") or 0, reverse=True)
    return items


def _clear_translation(directory: Path) -> None:
    chunks = chunk_dir(directory)
    if chunks.exists():
        for path in chunks.glob("*.txt"):
            path.unlink()
    vi = translated_transcript(directory)
    if vi.exists():
        vi.unlink()
    listen = _listen_dir(directory)
    if listen.exists():
        shutil.rmtree(listen)
    stamp = directory / "vi" / "listen.stamp"
    if stamp.exists():
        stamp.unlink()
    raw = directory / "vi" / "raw.txt"
    if raw.exists():
        raw.unlink()
    mark = natural_mark(directory)
    if mark.exists():
        mark.unlink()


def enqueue(
    url: str,
    glossary: str | None = None,
    force: bool = False,
    mode: str = "auto",
    title: str | None = None,
) -> dict:
    if mode not in {"auto", "en", "vi", "sangtacviet"}:
        mode = "auto"
    if mode == "sangtacviet" or parse_chapter_url(url):
        if parse_chapter_url(url) is None:
            raise PipelineError("Chế độ Sangtacviet cần link một chương hoặc trang truyện")
        meta = chapter_meta(url, title)
        mode = "sangtacviet"
    else:
        meta = fetch_meta(url)
        meta["source"] = "youtube"
    job_id = meta["id"]
    title_for_folder = None if meta.get("source") == "sangtacviet" else meta.get("title")
    directory = ensure_job_dir(job_id, title_for_folder)
    previous = directory / "meta.json"
    if meta.get("source") == "sangtacviet" and previous.exists():
        old = json.loads(previous.read_text(encoding="utf-8"))
        if old.get("chapterTitle"):
            meta["title"] = old.get("title") or meta["title"]
            meta["book"] = old.get("book") or ""
            meta["chapterTitle"] = old["chapterTitle"]
    (directory / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if glossary is not None:
        text = glossary.strip()
        (directory / "glossary.txt").write_text((text + "\n") if text else "", encoding="utf-8")

    if (directory / "status.json").exists():
        current = _read_status(job_id)
        if current.get("status") in {"queued", "running"}:
            return _public(job_id, with_text=True)
        saved_mode = current.get("mode") or "auto"
        needs_natural = _pending_han_viet(directory, current, mode)
        if current.get("status") == "done" and not force and saved_mode == mode and not needs_natural:
            return _public(job_id, with_text=True)

    if force:
        _clear_translation(directory)

    _write_status(
        job_id,
        url=meta["url"],
        title=meta["title"],
        channel=meta["channel"],
        status="queued",
        step="download",
        message="Đang chờ",
        done=0,
        total=0,
        error=None,
        force=force,
        mode=mode,
        source=meta.get("source") or "youtube",
    )
    _queue.put(job_id)
    return _public(job_id, with_text=True)


def enqueue_book(
    url: str,
    start: int,
    end: int,
    glossary: str | None = None,
    force: bool = False,
    mode: str = "sangtacviet",
) -> dict:
    chosen = chapters_in_range(url, start, end)
    first = None
    for row in chosen:
        label = row["title"] or f"Chương {row['number']}"
        job = enqueue(row["url"], glossary=glossary, force=force, mode=mode, title=label)
        if first is None:
            first = job
    if first is None:
        raise PipelineError("Không xếp được chương nào")
    first["batch"] = len(chosen)
    first["batchFrom"] = chosen[0]["number"]
    first["batchTo"] = chosen[-1]["number"]
    return first


def save_text(job_id: str, text: str, part: int = 1) -> dict:
    current = _read_status(job_id)
    if current.get("status") in {"queued", "running"}:
        raise PipelineError("Đang xử lý, chưa sửa được bản dịch")
    directory = _job_dir(job_id)
    files = _ensure_listen_parts(directory)
    if not files:
        raise PipelineError("Chưa có bản dịch để sửa")
    chosen = min(max(part, 1), len(files))
    body = text if text.endswith("\n") or text == "" else text + "\n"
    files[chosen - 1].write_text(body, encoding="utf-8")
    merged = "\n\n".join(path.read_text(encoding="utf-8").strip() for path in files if path.read_text(encoding="utf-8").strip())
    vi = translated_transcript(directory)
    vi.parent.mkdir(parents=True, exist_ok=True)
    payload = merged + "\n"
    vi.write_text(payload, encoding="utf-8")
    (directory / "vi" / "listen.stamp").write_text(str(vi.stat().st_size), encoding="utf-8")
    return _public(job_id, with_text=True, part=chosen)


def _remember_raw(directory: Path, text: str, *, force: bool) -> str:
    raw_path = directory / "vi" / "raw.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if force or not raw_path.exists() or raw_path.stat().st_size == 0:
        payload = text if text.endswith("\n") or text == "" else text + "\n"
        raw_path.write_text(payload, encoding="utf-8")
    return raw_path.read_text(encoding="utf-8")


def _pending_han_viet(directory: Path, status: dict, mode: str) -> bool:
    """Job đã xong nhưng lời tiếng Việt vẫn là Hán Việt, cần viết lại."""
    if natural_mark(directory).exists():
        return False
    if mode == "sangtacviet" or status.get("source") == "sangtacviet":
        return True
    if status.get("language") != "vi" and mode != "vi":
        return False
    raw = directory / "vi" / "raw.txt"
    path = raw if raw.exists() and raw.stat().st_size > 0 else translated_transcript(directory)
    if not path.exists() or path.stat().st_size == 0:
        return False
    return looks_han_viet(path.read_text(encoding="utf-8"))


def _finish_vietnamese(job_id: str, directory: Path, source: str, *, force: bool, on_progress, plain: str) -> None:
    if looks_han_viet(source):
        _write_status(
            job_id,
            step="translate",
            language="vi",
            message="Lời tiếng Việt đang là Hán Việt, đang viết lại cho dễ đọc...",
        )
        naturalize_chapter(directory, source, force=force, on_progress=on_progress)
        message = "Xong. Đã chuyển bản Hán Việt sang tiếng Việt đọc được."
    else:
        message = plain
    _write_status(job_id, status="done", step="done", language="vi", message=message, error=None)


def _load_youtube_vietnamese(job_id: str, url: str, directory: Path, *, force: bool, on_progress) -> str:
    raw_path = directory / "vi" / "raw.txt"
    if not force and raw_path.exists() and raw_path.stat().st_size > 0:
        return raw_path.read_text(encoding="utf-8")
    vi_text = directory / "vi" / "transcript.txt"
    try:
        text = fetch_captions(
            url,
            directory,
            lang="vi",
            force=force,
            on_progress=on_progress,
            grouped=True,
        )
    except NoCaptionsError:
        _write_status(job_id, step="transcribe", message="Không có phụ đề tiếng Việt. Đang nhận dạng giọng nói.")
        text, _detected = run_transcribe(
            url,
            directory,
            on_progress=on_progress,
            language="vi",
            dest=vi_text,
            force=force,
        )
    return _remember_raw(directory, text, force=True)


def _run_job(job_id: str) -> None:
    directory = _job_dir(job_id)
    status = _read_status(job_id)
    force = bool(status.get("force"))
    mode = status.get("mode") or "auto"
    if mode not in {"auto", "en", "vi", "sangtacviet"}:
        mode = "auto"
    url = status.get("url") or f"https://youtu.be/{job_id}"

    def on_progress(message: str, done: int | None = None, total: int | None = None) -> None:
        patch = {"message": message}
        if done is not None:
            patch["done"] = done
        if total is not None:
            patch["total"] = total
        _write_status(job_id, **patch)

    if status.get("mode") == "sangtacviet" or status.get("source") == "sangtacviet" or parse_chapter_url(url):
        try:
            _write_status(job_id, status="running", step="download", language="vi", error=None, force=False)
            fetch_chapter(url, directory, force=force, on_progress=on_progress)
            meta_path = directory / "meta.json"
            title = _read_status(job_id).get("title")
            if meta_path.exists():
                saved = json.loads(meta_path.read_text(encoding="utf-8"))
                title = saved.get("title") or title
            _write_status(
                job_id,
                status="done",
                step="done",
                language="vi",
                title=title,
                message="Xong. Đã chuyển bản Hán Việt sang tiếng Việt đọc được.",
                error=None,
            )
        except Exception as exc:
            _write_status(job_id, status="error", error=str(exc), message=str(exc))
        return

    try:
        _write_status(job_id, status="running", step="download", error=None, force=False, message="Đang nhận ngôn ngữ...")
        language = mode
        if mode == "auto":
            language = probe_language(url)
            label = {"vi": "tiếng Việt", "en": "tiếng Anh"}.get(language, "không rõ, sẽ thử tiếng Anh")
            _write_status(job_id, language=language, message=f"Nhận ra {label}.")
            if language != "vi":
                language = "en"
        if language == "vi":
            _write_status(job_id, step="download", language="vi", message="Đang lấy lời tiếng Việt...")
            source = _load_youtube_vietnamese(
                job_id,
                url,
                directory,
                force=force or status.get("language") != "vi",
                on_progress=on_progress,
            )
            _finish_vietnamese(
                job_id,
                directory,
                source,
                force=force,
                on_progress=on_progress,
                plain="Xong. Video tiếng Việt, không cần dịch.",
            )
            return

        try:
            fetch_english_transcript(url, directory, force=force, on_progress=on_progress, grouped=True)
        except NoCaptionsError as exc:
            _write_status(job_id, step="transcribe", message=str(exc))
            _text, detected = run_transcribe(
                url,
                directory,
                on_progress=on_progress,
                language=None if mode == "auto" else "en",
                force=force,
            )
            if mode == "auto" and str(detected).startswith("vi"):
                source = _remember_raw(directory, _text, force=force or status.get("language") != "vi")
                _finish_vietnamese(
                    job_id,
                    directory,
                    source,
                    force=force,
                    on_progress=on_progress,
                    plain="Giọng tiếng Việt, không cần dịch.",
                )
                return
        _write_status(job_id, step="translate", language="en", message="Đang dịch sang tiếng Việt...")
        run_translation(directory, on_progress=on_progress)
        _write_status(job_id, status="done", step="done", language="en", message="Xong. Có thể sửa chữ rồi tạo audio.", error=None)
    except Exception as exc:
        _write_status(job_id, status="error", error=str(exc), message=str(exc))


def _worker() -> None:
    while True:
        job_id = _queue.get()
        try:
            _run_job(job_id)
        except Exception as exc:
            print(f"Job {job_id} lỗi: {exc}", flush=True)
            try:
                _write_status(job_id, status="error", error=str(exc), message=str(exc))
            except Exception:
                pass
        finally:
            _queue.task_done()


def _resume_interrupted() -> None:
    for item in _list_jobs():
        if item.get("status") in {"queued", "running"}:
            job_id = item.get("id")
            if not job_id:
                continue
            _write_status(job_id, status="queued", message="Chạy tiếp sau khi mở lại server")
            _queue.put(job_id)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[pipeline] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 8_000_000:
            raise PipelineError("Nội dung quá lớn")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise PipelineError("JSON phải là object")
        return data

    def _job_id(self, path: str) -> str | None:
        match = re.fullmatch(r"/pipeline/jobs/([A-Za-z0-9_-]{11,24})(?:/text)?", path)
        if not match:
            return None
        return match.group(1)

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/pipeline/health":
            self._send(200, {"ok": True})
            return
        if path == "/pipeline/jobs":
            self._send(200, {"jobs": _list_jobs()})
            return
        job_id = self._job_id(path)
        if job_id and path == f"/pipeline/jobs/{job_id}":
            try:
                query = parse_qs(urlparse(self.path).query)
                part = int((query.get("part") or ["1"])[0] or "1")
                full = (query.get("full") or ["0"])[0] == "1"
                self._send(200, _public(job_id, with_text=True, part=part, full=full))
            except FileNotFoundError:
                self._send(404, {"error": "Không thấy job"})
            except ValueError:
                self._send(400, {"error": "Số phần không hợp lệ"})
            return
        self._send(404, {"error": "Không thấy đường dẫn"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != "/pipeline/jobs":
            self._send(404, {"error": "Không thấy đường dẫn"})
            return
        try:
            body = self._read_json()
            url = str(body.get("url") or "").strip()
            if not url:
                raise PipelineError("Thiếu link")
            if parse_book_url(url) is None and parse_chapter_url(url) is None:
                video_id(url)
            glossary = body.get("glossary")
            if glossary is not None:
                glossary = str(glossary)
            mode = str(body.get("mode") or "auto")
            if parse_book_url(url):
                start = int(body.get("fromChapter") or 1)
                end = int(body.get("toChapter") or start)
                job = enqueue_book(url, start, end, glossary=glossary, force=bool(body.get("force")), mode=mode)
            else:
                job = enqueue(url, glossary=glossary, force=bool(body.get("force")), mode=mode)
            self._send(200, job)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except PipelineError as exc:
            self._send(400, {"error": str(exc)})
        except json.JSONDecodeError:
            self._send(400, {"error": "JSON không đọc được"})

    def do_PUT(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        match = re.fullmatch(r"/pipeline/jobs/([A-Za-z0-9_-]{11,24})/text", path)
        if not match:
            self._send(404, {"error": "Không thấy đường dẫn"})
            return
        try:
            body = self._read_json()
            if "text" not in body:
                raise PipelineError("Thiếu bản dịch")
            part = int(body.get("part") or 1)
            job = save_text(match.group(1), str(body.get("text") or ""), part)
            self._send(200, job)
        except FileNotFoundError:
            self._send(404, {"error": "Không thấy job"})
        except ValueError:
            self._send(400, {"error": "Số phần không hợp lệ"})
        except PipelineError as exc:
            self._send(409, {"error": str(exc)})
        except json.JSONDecodeError:
            self._send(400, {"error": "JSON không đọc được"})


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    JOBS.mkdir(exist_ok=True)
    threading.Thread(target=_worker, name="pipeline-worker", daemon=True).start()
    _resume_interrupted()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Pipeline nghe tại http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dừng server", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
