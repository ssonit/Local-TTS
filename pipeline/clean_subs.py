"""Làm sạch phụ đề tự động: bỏ dòng lặp kiểu chữ chạy của YouTube."""

from __future__ import annotations

import json
import re
from pathlib import Path

_TAG = re.compile(r"<[^>]+>")
_MUSIC = re.compile(r"\[(?:music|applause|laughter|nhạc)\]", re.I)
_TIME_IN_TEXT = re.compile(r"<\d{2}:\d{2}:\d{2}\.\d{3}>")


def _words(text: str) -> list[str]:
    text = _TIME_IN_TEXT.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = _MUSIC.sub(" ", text)
    text = text.replace(">>", " ")
    return text.split()


def _collapse_stutter(words: list[str]) -> list[str]:
    """Ba lần cùng một từ liên tiếp trở lên thì giữ một. Đôi từ ('very very') giữ nguyên."""
    out: list[str] = []
    i = 0
    while i < len(words):
        j = i + 1
        while j < len(words) and words[j].lower() == words[i].lower():
            j += 1
        if j - i >= 3:
            out.append(words[i])
        else:
            out.extend(words[i:j])
        i = j
    return out


def _append_cue(words: list[str], cue_words: list[str]) -> None:
    if not cue_words:
        return
    if not words:
        words.extend(cue_words)
        return

    recent = [w.lower() for w in words[-80:]]
    incoming = [w.lower() for w in cue_words]
    if len(incoming) <= len(recent):
        for i in range(len(recent) - len(incoming) + 1):
            if recent[i : i + len(incoming)] == incoming:
                return

    max_n = min(len(words), len(cue_words), 50)
    word_l = [w.lower() for w in words]
    for n in range(max_n, 0, -1):
        if word_l[-n:] == incoming[:n]:
            words.extend(cue_words[n:])
            return

    window = word_l[-60:]
    best_n = 0
    best_i = 0
    upper = min(len(window), len(incoming))
    for n in range(upper, 5, -1):
        suffix = window[-n:]
        for i in range(0, len(incoming) - n + 1):
            if incoming[i : i + n] == suffix:
                best_n = n
                best_i = i
                break
        if best_n >= 8:
            break
    if best_n >= 6:
        words.extend(cue_words[best_i + best_n :])
        return
    words.extend(cue_words)


def clean_cues(cues: list[str]) -> str:
    paragraphs: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        words: list[str] = []
        for cue in buf:
            _append_cue(words, _words(cue))
        buf.clear()
        words = _collapse_stutter(words)
        if words:
            paragraphs.append(" ".join(words))

    for cue in cues:
        if cue.strip() in {"", "\n"}:
            flush()
            continue
        parts = re.split(r"\n+", cue)
        for i, part in enumerate(parts):
            if not part.strip():
                flush()
                continue
            buf.append(part)
            if i < len(parts) - 1:
                flush()
    flush()
    return "\n\n".join(paragraphs).strip() + ("\n" if paragraphs else "")


def cues_from_json3(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cues: list[str] = []
    for event in data.get("events") or []:
        segs = event.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segs)
        if text:
            cues.append(text)
    return cues


def cues_from_srt_or_vtt(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    cues: list[str] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = []
        for line in block.splitlines():
            s = line.strip()
            if not s or s.startswith("WEBVTT") or s.startswith("Kind:") or s.startswith("Language:"):
                continue
            if "-->" in s or re.fullmatch(r"\d+", s):
                continue
            lines.append(s)
        if lines:
            cues.append(" ".join(lines))
    return cues


def load_cues(path: Path) -> list[str]:
    name = path.name.lower()
    if name.endswith(".json3") or name.endswith(".json"):
        return cues_from_json3(path)
    return cues_from_srt_or_vtt(path)


def word_count(text: str) -> int:
    return len(text.split())


def _self_test() -> None:
    cues = [
        "I went to the store",
        "I went to the store to buy milk",
        "to the store to buy milk and bread",
        "buy milk and bread",
        "and bread for dinner",
        "\n",
        "The the the next day I left",
    ]
    text = clean_cues(cues)
    paras = text.strip().split("\n\n")
    assert paras[0] == "I went to the store to buy milk and bread for dinner", paras[0]
    assert "the the" not in paras[1].lower()
    assert paras[1].lower().count("the") == 1
    print("clean_subs ok")


if __name__ == "__main__":
    _self_test()
