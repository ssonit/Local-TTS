"""Video không có phụ đề: tải audio rồi nhận dạng giọng nói bằng faster-whisper.

Audio được cắt thành từng đoạn, mỗi đoạn ghi một file, chạy lại thì làm tiếp.

  python -m pipeline.transcribe https://youtu.be/xxxx -o outputs/ten
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .layout import source_transcript
from .translate import paragraphs_to_chunks

# Client visionos cho phép đọc byte bất kỳ; client mặc định chỉ cho đọc tuần tự và bị bóp băng thông.
_YT_CLIENT = "youtube:player_client=visionos"


def report(on_progress, message: str, done: int | None = None, total: int | None = None) -> None:
    print(message, flush=True)
    if on_progress:
        on_progress(message, done, total)


def _seconds(stamp: str) -> float:
    total = 0.0
    for part in stamp.split(":"):
        total = total * 60 + float(part)
    return total


def _fetch(url: str, headers: dict, start: int, end: int) -> bytes:
    out = bytearray()
    step = 2_000_000
    for pos in range(start, end + 1, step):
        stop = min(end, pos + step - 1)
        req = urllib.request.Request(f"{url}&range={pos}-{stop}", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            out += resp.read()
    return bytes(out)


def _boxes(data: bytes):
    i = 0
    while i + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[i : i + 8])
        if size < 8:
            return
        yield i, size, kind
        i += size


def download_section(url: str, out_path: Path, section: str) -> None:
    """Tải một khoảng thời gian từ luồng m4a DASH nhờ bảng chỉ mục sidx ở đầu file."""
    start_s, end_s = (_seconds(s) for s in section.split("-"))
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--no-update", "--extractor-args", _YT_CLIENT, "-f", "139", "-j", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    info = json.loads(proc.stdout)
    media, headers = info["url"], info.get("http_headers", {})

    head = _fetch(media, headers, 0, 65_535)
    init_end = sidx_at = sidx_size = None
    for pos, size, kind in _boxes(head):
        if kind == b"moov":
            init_end = pos + size
        if kind == b"sidx":
            sidx_at, sidx_size = pos, size
            break
    if init_end is None or sidx_at is None:
        raise RuntimeError("Luồng m4a không có sidx, không cắt đoạn được")
    sidx = _fetch(media, headers, sidx_at, sidx_at + sidx_size - 1)

    version = sidx[8]
    timescale = struct.unpack(">I", sidx[16:20])[0]
    if version == 0:
        pts, first = struct.unpack(">II", sidx[20:28])
        p = 28
    else:
        pts, first = struct.unpack(">QQ", sidx[20:36])
        p = 36
    count = struct.unpack(">H", sidx[p + 2 : p + 4])[0]
    p += 4
    offset = sidx_at + sidx_size + first
    t = pts / timescale
    byte_from = byte_to = None
    clip_start = 0.0
    for _ in range(count):
        ref, dur, _sap = struct.unpack(">III", sidx[p : p + 12])
        p += 12
        size = ref & 0x7FFFFFFF
        seg_end = t + dur / timescale
        if byte_from is None and seg_end > start_s:
            byte_from, clip_start = offset, t
        if byte_from is not None and t < end_s:
            byte_to = offset + size - 1
        offset += size
        t = seg_end
    if byte_from is None or byte_to is None:
        raise RuntimeError(f"Khoảng {section} nằm ngoài độ dài audio")

    raw = out_path.with_suffix(".raw.m4a")
    raw.write_bytes(head[:init_end] + _fetch(media, headers, byte_from, byte_to))
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(raw),
            "-ss",
            f"{start_s - clip_start:.3f}",
            "-t",
            f"{end_s - start_s:.3f}",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(out_path),
        ],
        check=True,
    )
    raw.unlink()


def download_audio(url: str, out_dir: Path, section: str | None) -> Path:
    found = [p for p in out_dir.glob("audio.*") if not p.name.endswith(".part")]
    if found:
        return found[0]
    if section:
        out = out_dir / "audio.wav"
        download_section(url, out, section)
        return out
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-update",
        "--extractor-args",
        _YT_CLIENT,
        "-f",
        "ba[abr<=80]/ba",
        "-o",
        str(out_dir / "audio.%(ext)s"),
        url,
    ]
    subprocess.run(cmd, check=True)
    found = [p for p in out_dir.glob("audio.*") if not p.name.endswith(".part")]
    if not found:
        raise RuntimeError("yt-dlp chạy xong nhưng không thấy file audio")
    return found[0]


def split_audio(audio: Path, out_dir: Path, seconds: int) -> list[Path]:
    seg_dir = out_dir / "audio_parts"
    parts = sorted(seg_dir.glob("*" + audio.suffix))
    if parts:
        return parts
    seg_dir.mkdir(exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio),
        "-f",
        "segment",
        "-segment_time",
        str(seconds),
        "-c",
        "copy",
        str(seg_dir / ("%03d" + audio.suffix)),
    ]
    subprocess.run(cmd, check=True)
    return sorted(seg_dir.glob("*" + audio.suffix))


def transcribe_part(model, path: Path, pause: float, language: str | None = "vi") -> tuple[str, str]:
    segments, info = model.transcribe(
        str(path),
        language=language or None,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    detected = getattr(info, "language", None) or language or ""
    paragraphs: list[str] = []
    buf: list[str] = []
    last_end = 0.0
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        if buf and seg.start - last_end > pause:
            paragraphs.append(" ".join(buf))
            buf = []
        buf.append(text)
        last_end = seg.end
    if buf:
        paragraphs.append(" ".join(buf))
    return "\n\n".join(paragraphs), detected


def merge(out_dir: Path, text_dir: Path, chunk_words: int) -> None:
    texts = [p.read_text(encoding="utf-8").strip() for p in sorted(text_dir.glob("*.txt"))]
    full = "\n\n".join(t for t in texts if t) + "\n"
    (out_dir / "transcript.txt").write_text(full, encoding="utf-8")
    chunk_dir = out_dir / "chunks"
    chunk_dir.mkdir(exist_ok=True)
    for old in chunk_dir.glob("*.txt"):
        old.unlink()
    chunks = paragraphs_to_chunks(full, chunk_words)
    for n, chunk in enumerate(chunks):
        (chunk_dir / f"{n:04d}.txt").write_text(chunk + "\n", encoding="utf-8")
    print(f"Gộp {len(texts)} phần -> transcript.txt ({len(full.split()):,} từ), {len(chunks)} chunk")


def _whisper(model_name: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Chưa cài faster-whisper. Chạy: pip install -r pipeline/requirements.txt"
        ) from exc

    try:
        return WhisperModel(model_name, device="cuda", compute_type="float16")
    except Exception as exc:
        print(f"CUDA không dùng được ({exc}). Chuyển sang CPU.", flush=True)
        return WhisperModel(model_name, device="cpu", compute_type="int8")


def run_transcribe(
    url: str,
    job_dir: Path,
    *,
    on_progress=None,
    language: str | None = "en",
    model_name: str = "large-v3-turbo",
    segment_minutes: int = 60,
    pause: float = 1.5,
    dest: Path | None = None,
    force: bool = False,
) -> tuple[str, str]:
    """Nhận dạng giọng nói. Audio nằm trong asr/. Tiếng Việt ghi vào vi/, tiếng Anh vào en/."""
    job_dir.mkdir(parents=True, exist_ok=True)
    asr_dir = job_dir / "asr"
    text_dir = asr_dir / "parts"
    if force and text_dir.exists():
        for old in text_dir.glob("*.txt"):
            old.unlink()
    existing = dest or source_transcript(job_dir)
    if (
        not force
        and dest is not None
        and existing.exists()
        and existing.stat().st_size > 0
        and text_dir.exists()
        and any(text_dir.glob("*.txt"))
    ):
        report(on_progress, "Đã có bản nhận dạng, bỏ qua.")
        return existing.read_text(encoding="utf-8"), language or ""

    report(on_progress, "Không có phụ đề. Đang nhận dạng giọng nói — bước này lâu.")
    text_dir.mkdir(parents=True, exist_ok=True)
    audio = download_audio(url, asr_dir, None)
    parts = split_audio(audio, asr_dir, segment_minutes * 60)
    report(on_progress, f"Audio {len(parts)} phần.", 0, len(parts))
    model = _whisper(model_name)
    for n, part in enumerate(parts):
        out = text_dir / f"{n:03d}.txt"
        if out.exists() and out.stat().st_size > 0:
            continue
        report(on_progress, f"Nhận dạng phần {n + 1}/{len(parts)}", n, len(parts))
        started = time.time()
        text, part_lang = transcribe_part(model, part, pause, language=language)
        if part_lang:
            language = part_lang
        out.write_text(text + "\n", encoding="utf-8")
        report(
            on_progress,
            f"Phần {n + 1}/{len(parts)} xong trong {(time.time() - started) / 60:.1f} phút",
            n + 1,
            len(parts),
        )

    texts = [p.read_text(encoding="utf-8").strip() for p in sorted(text_dir.glob("*.txt"))]
    full = "\n\n".join(t for t in texts if t) + "\n"
    detected = language or "en"
    if dest is None:
        dest = job_dir / "vi" / "transcript.txt" if detected.startswith("vi") else source_transcript(job_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(full, encoding="utf-8")
    report(on_progress, f"Nhận dạng xong ({detected}): {len(full.split()):,} từ.", len(parts), len(parts))
    return full, detected


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Tải audio và nhận dạng giọng nói tiếng Việt")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", required=True, help="thư mục kết quả")
    parser.add_argument("--section", default=None, help="chỉ lấy một đoạn, vd 27:20:00-27:40:00")
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda", help="cuda cần cublas/cudnn của CUDA 12")
    parser.add_argument("--segment-minutes", type=int, default=60)
    parser.add_argument("--parts", type=int, default=0, help="chỉ làm thêm N phần audio, để thử")
    parser.add_argument("--pause", type=float, default=1.5, help="khoảng lặng (giây) để xuống đoạn")
    parser.add_argument("--chunk-words", type=int, default=1000)
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    text_dir = out_dir / "parts"
    text_dir.mkdir(exist_ok=True)

    audio = download_audio(args.url, out_dir, args.section)
    parts = split_audio(audio, out_dir, args.segment_minutes * 60)
    print(f"Audio {audio.name}: {len(parts)} phần")

    from faster_whisper import WhisperModel

    compute = "float16" if args.device == "cuda" else "int8"
    model = WhisperModel(args.model, device=args.device, compute_type=compute)
    done_new = 0
    for n, part in enumerate(parts):
        out = text_dir / f"{n:03d}.txt"
        if out.exists() and out.stat().st_size > 0:
            continue
        if args.parts and done_new >= args.parts:
            break
        start = time.time()
        text, _detected = transcribe_part(model, part, args.pause, language="vi")
        out.write_text(text + "\n", encoding="utf-8")
        done_new += 1
        print(f"Phần {n + 1}/{len(parts)} xong trong {(time.time() - start) / 60:.1f} phút")

    merge(out_dir, text_dir, args.chunk_words)


if __name__ == "__main__":
    main()
