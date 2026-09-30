"""Dịch transcript.txt sang tiếng Việt bằng LLM, từng đoạn, chạy lại được.

Dán API key vào file .env ở gốc repo (dòng GEMINI_API_KEY=...).
Model mặc định là gemini-3.1-flash-lite.

  python -m pipeline.translate --dir jobs/<video_id>
  python -m pipeline.translate --dir jobs/<video_id> --limit 5
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from .errors import PipelineError
from .layout import chunk_dir, source_transcript, translated_transcript

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"
_PLACEHOLDER = {"", "dan_key_vao_day", "dán_key_vào_đây"}

SYSTEM_TEMPLATE = """Bạn là biên dịch light novel. Dịch transcript tiếng Anh sang tiếng Việt tự nhiên, giọng kể chuyện, đọc thành tiếng dễ nghe.
Transcript lấy từ phụ đề tự động: thiếu dấu câu, có thể sai chính tả. Hãy tự thêm dấu câu và chia đoạn.
Dịch đủ ý, không tóm tắt, không chú thích, không mở đầu bằng lời dẫn.
Đầu và cuối đoạn có thể bị cắt giữa câu: dịch phần có sẵn, đừng bịa nốt câu.
Giữ đúng bảng thuật ngữ và cách xưng hô. Một tên chỉ một cách viết.
Chỉ trả về bản dịch.

Bảng thuật ngữ:
{glossary}"""


def report(on_progress, message: str, done: int | None = None, total: int | None = None) -> None:
    print(message, flush=True)
    if on_progress:
        on_progress(message, done, total)


def load_keys() -> dict[str, str]:
    """Đọc file .env ở gốc repo. Biến môi trường đã có thì giữ nguyên."""
    keys: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value.lower() in _PLACEHOLDER:
                continue
            keys[name.strip()] = value
    return keys


def gemini_keys(keys: dict[str, str]) -> list[str]:
    found: list[str] = []
    for name, value in keys.items():
        upper = name.upper()
        if upper.startswith("GEMINI_API_KEY") or upper.startswith("GOOGLE_API_KEY"):
            if value not in found:
                found.append(value)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value and value not in found:
            found.append(value)
    return found


def _limit_kind(exc: Exception) -> str:
    message = str(exc).lower()
    if any(
        part in message
        for part in (
            "per day",
            "perday",
            "daily",
            "rpd",
            "requests per day",
            "exceeded your current quota",
            "check your plan and billing",
        )
    ):
        return "daily"
    if any(
        part in message
        for part in (
            "503",
            "unavailable",
            "high demand",
            "overloaded",
            "temporarily",
        )
    ):
        return "busy"
    if any(
        part in message
        for part in (
            "403",
            "permission_denied",
            "denied access",
        )
    ):
        return "denied"
    if any(
        part in message
        for part in (
            "400",
            "invalid_argument",
            "invalid argument",
            "404",
            "not found",
            "no longer available",
        )
    ):
        return "bad_model"
    if any(
        part in message
        for part in (
            "429",
            "resource_exhausted",
            "quota",
            "rate limit",
            "rate_limit",
        )
    ):
        return "rpm"
    return "other"


def _safe_err(exc: Exception) -> str:
    text = re.sub(r"AIza[\w\-]+", "AIza…", str(exc))
    return text[:300]


def api_key(keys: dict[str, str], *names: str) -> str:
    for name in names:
        if keys.get(name):
            return keys[name]
        if os.environ.get(name):
            return os.environ[name]
    return ""


def load_glossary(path: Path) -> str:
    if not path.exists():
        return "(không có)"
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines) or "(không có)"


def paragraphs_to_chunks(text: str, target_words: int) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    count = 0

    def flush() -> None:
        nonlocal count
        if buf:
            chunks.append("\n\n".join(buf))
            buf.clear()
            count = 0

    for para in paras:
        n = len(para.split())
        if count and count + n > target_words:
            flush()
        if n > target_words:
            words = para.split()
            for i in range(0, len(words), target_words):
                chunks.append(" ".join(words[i : i + target_words]))
            continue
        buf.append(para)
        count += n
    flush()
    return chunks


def context_tail(text: str, limit: int = 450) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    match = re.search(r"[.!?…][\"”']?\s+", cut)
    if match:
        cut = cut[match.end() :]
    return cut.strip()


def user_prompt(chunk: str, prev: str) -> str:
    if not prev:
        return f"Dịch đoạn sau:\n{chunk}"
    return (
        "Đoạn dịch liền trước (chỉ để nối mạch và cách xưng hô, KHÔNG dịch lại):\n"
        f"{prev}\n\n---\nDịch đoạn sau:\n{chunk}"
    )


class DailyQuota(Exception):
    pass


class Translator:
    def __init__(self, provider: str, model: str, keys: dict[str, str]) -> None:
        self.provider = provider
        self.model = model
        self._thinking_off = True
        self._key_index = 0
        self._gemini_keys: list[str] = []
        self._flash_models = [
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
            "gemini-3.8-flash",
            "gemini-flash-latest",
        ]
        self._model_tries = 0
        self._dropped_models: set[str] = set()
        if provider == "anthropic":
            import anthropic

            key = api_key(keys, "ANTHROPIC_API_KEY")
            if not key:
                raise PipelineError("Thiếu ANTHROPIC_API_KEY. Mở file .env và điền dòng đó.")
            self.client = anthropic.Anthropic(api_key=key)
        elif provider == "gemini":
            from google import genai

            self._genai = genai
            self._gemini_keys = gemini_keys(keys)
            if not self._gemini_keys:
                raise PipelineError(
                    "Thiếu GEMINI_API_KEY. Mở file .env ở gốc project và dán key vào dòng đó."
                )
            self.client = genai.Client(api_key=self._gemini_keys[0])
            if model not in self._flash_models:
                self._flash_models = [model] + self._flash_models
            else:
                self._flash_models = [model] + [m for m in self._flash_models if m != model]
            self.model = self._flash_models[0]
            self._all_flash_models = list(self._flash_models)
        else:
            raise PipelineError(f"Provider không hỗ trợ: {provider}")

    def _rotate_gemini_key(self, reason: str = "hết hạn mức ngày") -> bool:
        nxt = self._key_index + 1
        if nxt >= len(self._gemini_keys):
            return False
        self._key_index = nxt
        self.client = self._genai.Client(api_key=self._gemini_keys[nxt])
        self._dropped_models.clear()
        self._model_tries = 0
        self._thinking_off = True
        if getattr(self, "_all_flash_models", None):
            self._flash_models = list(self._all_flash_models)
            self.model = self._flash_models[0]
        print(f"Key {self._key_index}/{len(self._gemini_keys)} {reason}, chuyển sang key {nxt + 1}", flush=True)
        return True

    def _reset_gemini_keys(self) -> None:
        if self.provider != "gemini" or not self._gemini_keys:
            return
        self._key_index = 0
        self.client = self._genai.Client(api_key=self._gemini_keys[0])
        self._model_tries = 0
        if self._flash_models:
            self.model = self._flash_models[0]

    def _rotate_flash_model(self, *, drop: bool = False) -> bool:
        if self.provider != "gemini" or not self._flash_models:
            return False
        old = self.model
        if drop:
            self._dropped_models.add(old)
            self._flash_models = [m for m in self._flash_models if m not in self._dropped_models]
            print(f"Bỏ model lỗi {old}", flush=True)
        else:
            if self._model_tries >= len(self._flash_models) - 1:
                self._model_tries = 0
                return False
            self._flash_models = self._flash_models[1:] + [old]
            self._model_tries += 1
        if not self._flash_models:
            self._model_tries = 0
            return False
        self.model = self._flash_models[0]
        if not drop:
            print(f"Model {old} bận/lỗi, chuyển sang {self.model}", flush=True)
        else:
            print(f"Chuyển sang {self.model}", flush=True)
        return True

    def __call__(self, system: str, user: str, max_tokens: int) -> tuple[str, bool]:
        if self.provider == "anthropic":
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0.2,
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(block.text for block in resp.content if getattr(block, "text", "")).strip()
            return text, resp.stop_reason == "max_tokens"

        from google.genai import types

        config_kwargs = dict(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=0.2,
        )
        if self._thinking_off:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except Exception as exc:
            msg = str(exc).lower()
            if self._thinking_off and (
                "thinking" in msg or "invalid_argument" in msg or "invalid argument" in msg
            ):
                self._thinking_off = False
                return self(system, user, max_tokens)
            kind = _limit_kind(exc)
            if kind == "daily" and self._rotate_gemini_key():
                return self(system, user, max_tokens)
            if kind == "daily":
                raise DailyQuota(_safe_err(exc))
            if kind == "denied" and self._rotate_gemini_key("bị từ chối truy cập"):
                return self(system, user, max_tokens)
            if kind == "bad_model" and self._rotate_flash_model(drop=True):
                return self(system, user, max_tokens)
            if kind in {"busy", "rpm"} and self._rotate_flash_model():
                return self(system, user, max_tokens)
            self._model_tries = 0
            raise
        self._model_tries = 0
        text = (resp.text or "").strip()
        truncated = False
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            reason = str(getattr(candidates[0], "finish_reason", ""))
            truncated = "MAX_TOKENS" in reason
        return text, truncated


def call_with_retry(
    translate: Translator,
    system: str,
    user: str,
    max_tokens: int,
    on_progress=None,
) -> tuple[str, bool]:
    daily_waits = 0
    attempt = 0
    while True:
        try:
            return translate(system, user, max_tokens)
        except DailyQuota as exc:
            daily_waits += 1
            wait = 600 if daily_waits < 3 else 1800
            report(
                on_progress,
                f"Hết hạn mức Gemini ({daily_waits}). Giữ các đoạn đã dịch, chờ {wait // 60} phút rồi làm tiếp.",
            )
            print(f"Chi tiết: {exc}", flush=True)
            time.sleep(wait)
            translate._reset_gemini_keys()
            attempt = 0
            continue
        except PipelineError:
            raise
        except Exception as exc:
            attempt += 1
            kind = _limit_kind(exc)
            if kind == "busy":
                wait = min(90, 20 * attempt)
                label = "máy chủ bận"
            elif kind == "rpm":
                wait = 25
                label = "chạm giới hạn"
            elif kind == "bad_model":
                wait = 3
                label = "model lỗi"
            else:
                wait = min(60, 8 * attempt)
                label = "lỗi"
            report(on_progress, f"{label}, thử lại sau {wait}s: {_safe_err(exc)}")
            time.sleep(wait)
            if attempt >= 20:
                raise PipelineError("Lỗi dịch liên tục. Chạy lại để làm tiếp các đoạn chưa có file.") from exc


def translate_piece(
    translate: Translator,
    system: str,
    chunk: str,
    prev: str,
    max_tokens: int,
    on_progress=None,
) -> str:
    text, truncated = call_with_retry(
        translate, system, user_prompt(chunk, prev), max_tokens, on_progress=on_progress
    )
    words = chunk.split()
    if truncated and len(words) > 280:
        mid = len(words) // 2
        print(f"  đoạn bị cắt cụt, tách đôi ({len(words)} từ)", flush=True)
        left = translate_piece(translate, system, " ".join(words[:mid]), prev, max_tokens, on_progress)
        right = translate_piece(
            translate, system, " ".join(words[mid:]), context_tail(left), max_tokens, on_progress
        )
        return f"{left}\n\n{right}".strip()
    if not text:
        raise PipelineError("API trả về rỗng. Chạy lại để làm tiếp.")
    return text


def merge_files(out_dir: Path, merged: Path, *, quiet: bool = False) -> None:
    parts = []
    files = sorted(out_dir.glob("*.txt"))
    for path in files:
        parts.append(path.read_text(encoding="utf-8").strip())
    body = "\n\n".join(p for p in parts if p) + "\n"
    tmp = merged.with_suffix(".txt.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(merged)
    if not quiet:
        print(f"Gộp {len(files)} đoạn -> {merged.name}", flush=True)


def run_translation(
    directory: Path,
    *,
    on_progress=None,
    limit: int = 0,
    chunk_words: int = 1000,
    sleep_s: float = 1.0,
    provider: str = "gemini",
    model: str | None = None,
) -> str:
    """Dịch en/transcript.txt thành vi/transcript.txt. Bỏ qua chunk đã có."""
    directory = directory.resolve()
    src = source_transcript(directory)
    glossary_path = directory / "glossary.txt"
    out_dir = chunk_dir(directory)
    merged = translated_transcript(directory)
    if not src.exists():
        raise PipelineError("Chưa có transcript.txt.")

    chosen = model or ("gemini-3.1-flash-lite" if provider == "gemini" else "claude-haiku-4-5-20251001")
    if provider == "gemini" and "flash" not in chosen.lower():
        print(f"{chosen} không phải Flash. Chuyển về gemini-3.1-flash-lite.", flush=True)
        chosen = "gemini-3.1-flash-lite"
    system = SYSTEM_TEMPLATE.format(glossary=load_glossary(glossary_path))
    chunks = paragraphs_to_chunks(src.read_text(encoding="utf-8"), chunk_words)
    out_dir.mkdir(parents=True, exist_ok=True)
    translate = Translator(provider, chosen, load_keys())
    extra = ""
    if provider == "gemini":
        extra = f", {len(translate._gemini_keys)} key"
    total = len(chunks)
    report(on_progress, f"{provider} / {chosen}{extra} — {total} đoạn", 0, total)

    done_new = 0
    prev = ""
    finished = 0
    for n, chunk in enumerate(chunks):
        path = out_dir / f"{n:04d}.txt"
        if path.exists() and path.stat().st_size > 0:
            prev = context_tail(path.read_text(encoding="utf-8"))
            finished += 1
            continue
        if limit and done_new >= limit:
            break
        report(on_progress, f"Đang dịch đoạn {n + 1}/{total}", finished, total)
        out = translate_piece(translate, system, chunk, prev, max_tokens=8192, on_progress=on_progress)
        path.write_text(out.strip() + "\n", encoding="utf-8")
        prev = context_tail(out)
        done_new += 1
        finished += 1
        merge_files(out_dir, merged, quiet=True)
        report(on_progress, f"Dịch {finished}/{total}", finished, total)
        if sleep_s:
            time.sleep(sleep_s)

    if finished:
        merge_files(out_dir, merged, quiet=True)
    if not merged.exists():
        raise PipelineError("Chưa dịch được đoạn nào.")
    report(on_progress, f"Dịch xong {finished}/{total}", finished, total)
    return merged.read_text(encoding="utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Dịch transcript.txt sang tiếng Việt bằng LLM")
    parser.add_argument("--provider", choices=("gemini", "anthropic"), default="gemini")
    parser.add_argument("--model", default=None)
    parser.add_argument("--chunk-words", type=int, default=1000)
    parser.add_argument("--limit", type=int, default=0, help="chỉ dịch thêm N đoạn mới, để thử văn phong")
    parser.add_argument("--sleep", type=float, default=1.0, help="giây nghỉ giữa các request")
    parser.add_argument("--dir", default=".", help="thư mục job (en/transcript.txt, glossary.txt, vi/)")
    args = parser.parse_args()
    try:
        run_translation(
            Path(args.dir),
            limit=args.limit,
            chunk_words=args.chunk_words,
            sleep_s=args.sleep,
            provider=args.provider,
            model=args.model,
        )
    except PipelineError as exc:
        sys.exit(str(exc))
    if args.limit:
        print("Đã dịch một phần. Bỏ --limit để chạy hết.")


if __name__ == "__main__":
    main()
