"""Dịch transcript.txt sang tiếng Việt bằng LLM, từng đoạn, chạy lại được.

Dán API key vào file .env ở gốc repo (GEMINI_API_KEY hoặc DEEPSEEK_API_KEY).
Model mặc định là gemini-3.1-flash-lite. DeepSeek trả phí dùng deepseek-flash.
DeepSeek trên máy (provider deepseek_local) gọi server Deepseek-API ở localhost, không cần key.

  python -m pipeline.translate --dir jobs/<video_id>
  python -m pipeline.translate --dir jobs/<video_id> --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .errors import PipelineError
from .layout import chunk_dir, source_transcript, translated_transcript

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"
_PLACEHOLDER = {"", "dan_key_vao_day", "dán_key_vào_đây"}
# deepseek-flash hiện do DeepSeek-V4.1-Flash phục vụ. Đổi bằng DEEPSEEK_MODEL nếu cần bản Pro.
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
# Server repo Deepseek-API. deepseek-chat là bản nhanh; deepseek-expert chậm hơn.
DEEPSEEK_LOCAL_MODEL = "deepseek-chat"
DEEPSEEK_LOCAL_URL = "http://127.0.0.1:8000/v1/chat/completions"
PROVIDERS = ("gemini", "anthropic", "deepseek", "deepseek_local")

SYSTEM_TEMPLATE = """Bạn là biên dịch light novel. Dịch transcript tiếng Anh sang tiếng Việt tự nhiên, giọng kể chuyện, đọc thành tiếng dễ nghe.
Transcript lấy từ phụ đề tự động: thiếu dấu câu, có thể sai chính tả. Hãy tự thêm dấu câu và chia đoạn.
Dịch đủ ý, không tóm tắt, không chú thích, không mở đầu bằng lời dẫn.
Đầu và cuối đoạn có thể bị cắt giữa câu: dịch phần có sẵn, đừng bịa nốt câu.
Viết tiếng Việt đời thường. Tránh văn Hán Việt: đừng dùng "tuy nhiên, thực sự, quả nhiên, hoàn toàn, vô cùng, lập tức, không khỏi, đột nhiên, một cách, khả năng, biểu cảm, khoảnh khắc, đối với, ngoài ra, bởi vì, do đó, vì vậy". Viết "nhưng, thật, rất, liền, bỗng, cứ, có thể, nét mặt, lúc, với, thêm nữa, vì, nên".
Giữ đúng bảng thuật ngữ và cách xưng hô. Một tên chỉ một cách viết.
Chỉ trả về bản dịch.

Bảng thuật ngữ:
{glossary}"""

SPOKEN_TEMPLATE = """Bạn là biên tập light novel. Đoạn dưới đã là tiếng Việt nhưng quá Hán Việt, đọc thành tiếng nghe cứng.
Viết lại bằng tiếng Việt đời thường, giọng kể chuyện, dễ nghe. Giữ đủ sự việc. Câu được ngắn hơn bản dịch máy, nhưng đừng bỏ tình tiết.
Đổi chữ sáo sang chữ nói: tuy nhiên -> nhưng, quả thực / thực sự / quả nhiên -> thật, hoàn toàn -> hẳn, vô cùng -> rất, lập tức -> liền, không khỏi -> bỏ đi rồi viết thẳng hành động, đột nhiên -> bỗng, một cách -> cứ, khả năng -> có thể, biểu cảm -> nét mặt, khoảnh khắc -> lúc, đối với -> với, ngoài ra / hơn nữa -> thêm nữa, bởi vì -> vì, do đó / vì vậy -> nên, dường như -> hình như, rõ ràng -> rõ, toát lên -> lộ ra.
Nếu câu dịch sát từ tiếng Trung, sửa thành câu tiếng Việt nói được, đúng ngữ pháp. Đổi "lão sư" thành "cô" hoặc "thầy" theo người đó, "các ngươi" thành "các cậu" hoặc "mọi người", "một mặt" thành "vẻ mặt", "bất đắc dĩ" thành "đành", "căn bản là không" thành "chẳng". "liền" chỉ để nối hành động thì bỏ hoặc đổi thành "rồi".
Tên riêng viết theo bảng thuật ngữ. Bảng có cách viết khác thì đổi, không giữ dạng Hán Việt. Tên bị ngắt giữa dòng thì ghép lại, một người chỉ một cách viết.
Giữ nguyên ý, tên riêng, thuật ngữ truyện và cách xưng hô. Không tóm tắt, không thêm tình tiết, không chú thích.
Chỉ trả về đoạn đã viết lại.

Bảng thuật ngữ:
{glossary}"""

# Cụm hay làm bản dịch nghe như văn viết. Khớp cả cụm, cụm dài được tính trước.
_HAN_VIET_PHRASES = (
    "không chút do dự",
    "tập trung cao độ",
    "đáng kinh ngạc",
    "chiến lợi phẩm",
    "ngay lập tức",
    "tuy nhiên",
    "quả thực",
    "quả nhiên",
    "hoàn toàn",
    "vô cùng",
    "thực sự",
    "khả năng",
    "chức năng",
    "kích hoạt",
    "biện pháp",
    "tuyệt trần",
    "thanh lịch",
    "sang trọng",
    "tinh tế",
    "duyên dáng",
    "tự mãn",
    "đắm chìm",
    "nao núng",
    "đáng gờm",
    "cam chịu",
    "loại trừ",
    "xâm nhập",
    "bối cảnh",
    "đắc thắng",
    "sáng tỏ",
    "biểu cảm",
    "khoảnh khắc",
    "đối mặt",
    "đối với",
    "ngoài ra",
    "hơn nữa",
    "bởi vì",
    "do đó",
    "vì vậy",
    "dường như",
    "rõ ràng",
    "toát lên",
    "lập tức",
    "dẫu vậy",
    "thầm lặng",
    "tràn đầy",
    "điềm tĩnh",
    "dẫn dắt",
    "sực tỉnh",
    "trốn thoát",
    "một cách",
    "không khỏi",
    "đột nhiên",
    "nhanh chóng",
    "tất nhiên",
    "đáp lại",
    "lặng lẽ",
    "phản ứng",
    "xuất hiện",
    "đơn giản",
    "vấn đề",
    "chắc chắn",
    "bất đắc dĩ",
    "các ngươi",
    "một mặt",
    "căn bản",
    "lão sư",
)


def report(on_progress, message: str, done: int | None = None, total: int | None = None) -> None:
    print(message, flush=True)
    if on_progress:
        on_progress(message, done, total)


def publish_status(directory: Path, **patch) -> None:
    """Ghi status.json khi chạy từ dòng lệnh. Server workflow chỉ hiện job có file này."""
    directory = directory.resolve()
    path = directory / "status.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    job_id = directory.name.rsplit("_", 1)[-1]
    meta: dict = {}
    meta_path = directory / "meta.json"
    if meta_path.exists():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except json.JSONDecodeError:
            meta = {}
    data.setdefault("url", meta.get("url") or f"https://youtu.be/{job_id}")
    data.setdefault("title", meta.get("title") or job_id)
    data.setdefault("channel", meta.get("channel") or "")
    data.setdefault("provider", "gemini")
    data.setdefault("mode", "vi")
    data.setdefault("language", "vi")
    data.setdefault("error", None)
    data.update(patch)
    data["id"] = job_id
    data["external"] = True
    data["updated"] = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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
    text = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-…", text)
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


def alias_pairs(glossary: str) -> list[tuple[str, str]]:
    """Dòng 'A / B -> Tên' trong bảng thuật ngữ. Dòng không có dấu / chỉ là lời dặn cho model."""
    pairs: list[tuple[str, str]] = []
    for line in glossary.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "->" not in line or "/" not in line:
            continue
        left, right = line.split("->", 1)
        target = right.strip()
        if not target:
            continue
        for alias in left.split("/"):
            alias = alias.strip()
            if alias and alias.casefold() != target.casefold():
                pairs.append((alias, target))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def apply_alias_glossary(text: str, glossary: str) -> str:
    pairs = alias_pairs(glossary)
    if not text or not pairs:
        return text
    mapping: dict[str, str] = {}
    parts: list[str] = []
    for alias, target in pairs:
        mapping.setdefault(alias.casefold(), target)
        parts.append(r"\s+".join(re.escape(piece) for piece in alias.split()))
    pattern = re.compile(r"(?<!\w)(" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)

    def repl(match: re.Match[str]) -> str:
        return mapping[re.sub(r"\s+", " ", match.group(0)).casefold()]

    return pattern.sub(repl, text)


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


def spoken_prompt(chunk: str, prev: str) -> str:
    if not prev:
        return f"Viết lại đoạn sau:\n{chunk}"
    return (
        "Đoạn liền trước (chỉ để giữ cách xưng hô, KHÔNG viết lại):\n"
        f"{prev}\n\n---\nViết lại đoạn sau:\n{chunk}"
    )


def han_viet_hits(text: str) -> int:
    padded = f" {' '.join(text.lower().split())} "
    count = 0
    for phrase in sorted(_HAN_VIET_PHRASES, key=len, reverse=True):
        needle = f" {phrase} "
        found = padded.count(needle)
        if found:
            count += found
            padded = padded.replace(needle, " ")
    return count


def too_han_viet(text: str) -> bool:
    """Bản dịch đã là tiếng Việt nhưng dày chữ Hán Việt, cần viết lại cho dễ nghe."""
    words = [word for word in text.split() if word]
    hits = han_viet_hits(text)
    if hits < 4 or not words:
        return False
    return hits * 100 / len(words) >= 0.9


def _keep_rewrite(original: str, rewritten: str, *, low: float = 0.72) -> bool:
    old = len(original.split())
    new = len(rewritten.split())
    if new < 40 or not old:
        return False
    ratio = new / old
    return low <= ratio <= 1.35


def soften_han_viet(
    translate: Translator,
    system: str,
    text: str,
    prev: str,
    max_tokens: int,
    on_progress=None,
    *,
    low: float = 0.72,
) -> str:
    if not too_han_viet(text):
        return text
    report(on_progress, "Bản dịch quá Hán Việt, đang viết lại cho dễ nghe")
    try:
        rewritten, truncated = call_with_retry(
            translate, system, spoken_prompt(text, prev), max_tokens, on_progress=on_progress
        )
    except PipelineError as exc:
        if "trả về rỗng" not in str(exc):
            raise
        print("  bản viết lại rỗng, giữ bản vừa dịch", flush=True)
        return text
    rewritten = rewritten.strip()
    if truncated or not _keep_rewrite(text, rewritten, low=low):
        print(
            f"  bản viết lại lệch độ dài ({len(rewritten.split())}/{len(text.split())} từ), giữ bản gốc",
            flush=True,
        )
        return text
    return rewritten


def resolve_model(provider: str, model: str | None) -> str:
    keys = load_keys()
    if provider == "deepseek":
        return (model or keys.get("DEEPSEEK_MODEL") or os.environ.get("DEEPSEEK_MODEL") or DEEPSEEK_MODEL).strip()
    if provider == "deepseek_local":
        return (
            model
            or keys.get("DEEPSEEK_LOCAL_MODEL")
            or os.environ.get("DEEPSEEK_LOCAL_MODEL")
            or DEEPSEEK_LOCAL_MODEL
        ).strip()
    chosen = model or ("gemini-3.1-flash-lite" if provider == "gemini" else "claude-haiku-4-5-20251001")
    if provider == "gemini" and "flash" not in chosen.lower():
        print(f"{chosen} không phải Flash. Chuyển về gemini-3.1-flash-lite.", flush=True)
        return "gemini-3.1-flash-lite"
    return chosen


class DailyQuota(Exception):
    pass


class EmptyReply(Exception):
    """API nhận request nhưng không trả chữ. Thường là lỗi tạm, không phải hết hạn mức."""


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
        elif provider == "deepseek":
            key = api_key(keys, "DEEPSEEK_API_KEY")
            if not key:
                raise PipelineError(
                    "Thiếu DEEPSEEK_API_KEY. Mở file .env ở gốc project và dán key vào dòng đó."
                )
            self._deepseek_key = key
        elif provider == "deepseek_local":
            self.model = resolve_model("deepseek_local", model)
            self._deepseek_local_url = (
                keys.get("DEEPSEEK_LOCAL_URL")
                or os.environ.get("DEEPSEEK_LOCAL_URL")
                or DEEPSEEK_LOCAL_URL
            ).strip()
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

    def _deepseek_chat(self, system: str, user: str, max_tokens: int) -> tuple[str, bool]:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self._thinking_off:
            payload["thinking"] = {"type": "disabled"}
        req = urllib.request.Request(
            DEEPSEEK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._deepseek_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            lowered = detail.lower()
            if self._thinking_off and exc.code == 400 and "thinking" in lowered:
                self._thinking_off = False
                return self._deepseek_chat(system, user, max_tokens)
            if exc.code == 401:
                raise PipelineError("Key DeepSeek không đúng. Kiểm tra DEEPSEEK_API_KEY trong file .env.") from exc
            if exc.code == 402:
                raise PipelineError("Tài khoản DeepSeek hết số dư.") from exc
            if exc.code == 403:
                raise PipelineError("DeepSeek từ chối key này.") from exc
            raise RuntimeError(f"DeepSeek {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek không nối được: {exc.reason}") from exc

        body = json.loads(raw) if raw else {}
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        fenced = re.fullmatch(r"```(?:\w+)?\s*(.*?)\s*```", text, flags=re.S)
        if fenced:
            text = fenced.group(1).strip()
        truncated = str(choice.get("finish_reason") or "") == "length"
        if not text and not truncated:
            raise EmptyReply(str(choice.get("finish_reason") or "không rõ"))
        return text, truncated

    def _deepseek_local_chat(self, system: str, user: str, max_tokens: int) -> tuple[str, bool]:
        """Gọi server Deepseek-API trên máy. Server bỏ qua max_tokens."""
        del max_tokens
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        req = urllib.request.Request(
            self._deepseek_local_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            lowered = detail.lower()
            if exc.code == 503 or "login_required" in lowered:
                raise PipelineError(
                    "Chưa đăng nhập DeepSeek. Trong thư mục Deepseek-API chạy: python -m deepseek.auth"
                ) from exc
            if exc.code == 404:
                raise PipelineError(
                    f"Model {self.model} không có trên Deepseek-API. Dùng deepseek-chat hoặc deepseek-expert."
                ) from exc
            if exc.code == 429:
                raise RuntimeError(f"429 rate limit DeepSeek trên máy: {detail or exc.reason}") from exc
            raise RuntimeError(f"DeepSeek trên máy {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise PipelineError(
                "Chưa chạy Deepseek-API. Mở terminal trong thư mục Deepseek-API và chạy: python app.py"
            ) from exc

        body = json.loads(raw) if raw else {}
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        fenced = re.fullmatch(r"```(?:\w+)?\s*(.*?)\s*```", text, flags=re.S)
        if fenced:
            text = fenced.group(1).strip()
        if not text:
            raise EmptyReply(str(choice.get("finish_reason") or "không rõ"))
        return text, False

    def __call__(self, system: str, user: str, max_tokens: int) -> tuple[str, bool]:
        if self.provider == "deepseek":
            return self._deepseek_chat(system, user, max_tokens)
        if self.provider == "deepseek_local":
            return self._deepseek_local_chat(system, user, max_tokens)
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
            truncated = resp.stop_reason == "max_tokens"
            if not text and not truncated:
                raise EmptyReply(str(resp.stop_reason or "không rõ"))
            return text, truncated

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
        reason = ""
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            reason = str(getattr(candidates[0], "finish_reason", "") or "")
        truncated = "MAX_TOKENS" in reason
        if text or truncated:
            return text, truncated
        feedback = getattr(resp, "prompt_feedback", None)
        block = str(getattr(feedback, "block_reason", "") or "") if feedback is not None else ""
        if block.endswith("UNSPECIFIED"):
            block = ""
        detail = block or reason or "không rõ"
        detail = detail.replace("FinishReason.", "").replace("BlockReason.", "")
        raise EmptyReply(detail)


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
            wait = 30
            report(
                on_progress,
                f"Hết hạn mức Gemini ({daily_waits}). Giữ các đoạn đã dịch, chờ {wait} giây rồi làm tiếp.",
            )
            print(f"Chi tiết: {exc}", flush=True)
            time.sleep(wait)
            translate._reset_gemini_keys()
            attempt = 0
            continue
        except PipelineError:
            raise
        except EmptyReply as exc:
            reason = str(exc).upper()
            if any(flag in reason for flag in ("PROHIBITED", "SAFETY", "BLOCK", "RECITATION")):
                print(f"API chặn cả đoạn: {exc}", flush=True)
                raise PipelineError("API trả về rỗng. Chạy lại để làm tiếp.") from exc
            attempt += 1
            print(f"API rỗng: {exc}", flush=True)
            if attempt >= 5:
                raise PipelineError("API trả về rỗng. Chạy lại để làm tiếp.") from exc
            wait = min(20, 4 * attempt)
            rotated = translate._rotate_flash_model()
            hop = f", chuyển sang {translate.model}" if rotated else ""
            report(on_progress, f"API trả về rỗng ({exc}){hop}. Thử lại sau {wait}s")
            time.sleep(wait)
            continue
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


def _translate_halves(
    translate: Translator,
    system: str,
    chunk: str,
    prev: str,
    max_tokens: int,
    on_progress,
    why: str,
) -> str:
    words = chunk.split()
    mid = len(words) // 2
    print(f"  {why}, tách đôi ({len(words)} từ)", flush=True)
    left = translate_piece(translate, system, " ".join(words[:mid]), prev, max_tokens, on_progress)
    right = translate_piece(
        translate, system, " ".join(words[mid:]), context_tail(left), max_tokens, on_progress
    )
    return f"{left}\n\n{right}".strip()


def translate_piece(
    translate: Translator,
    system: str,
    chunk: str,
    prev: str,
    max_tokens: int,
    on_progress=None,
) -> str:
    words = chunk.split()
    try:
        text, truncated = call_with_retry(
            translate, system, user_prompt(chunk, prev), max_tokens, on_progress=on_progress
        )
    except PipelineError as exc:
        if "trả về rỗng" in str(exc) and len(words) > 80:
            return _translate_halves(
                translate, system, chunk, prev, max_tokens, on_progress, "API chặn cả đoạn"
            )
        raise
    if truncated and len(words) > 280:
        return _translate_halves(
            translate, system, chunk, prev, max_tokens, on_progress, "đoạn bị cắt cụt"
        )
    if not text and len(words) > 80:
        return _translate_halves(
            translate, system, chunk, prev, max_tokens, on_progress, "API trả về rỗng"
        )
    if not text:
        raise PipelineError("API trả về rỗng. Chạy lại để làm tiếp.")
    return text


def needs_spoken_rewrite(text: str) -> bool:
    """Phụ đề đã là tiếng Việt nhưng nhiều đoạn đặc như dịch máy từ tiếng Trung."""
    chunks = paragraphs_to_chunks(text, 1000)
    if not chunks:
        return False
    picks = {0, len(chunks) // 2, len(chunks) - 1}
    if len(chunks) > 6:
        picks.add(len(chunks) // 4)
        picks.add((3 * len(chunks)) // 4)
    flagged = sum(1 for i in picks if too_han_viet(chunks[i]))
    return flagged * 2 >= len(picks)


def spoken_source(directory: Path) -> Path:
    return directory / "vi" / "source.txt"


def ensure_spoken_source(directory: Path) -> Path | None:
    """Giữ bản phụ đề gốc. Trả về None nếu không cần viết lại."""
    source = spoken_source(directory)
    if source.exists() and source.stat().st_size > 0:
        return source
    transcript = translated_transcript(directory)
    if not transcript.exists() or transcript.stat().st_size == 0:
        raise PipelineError("Chưa có phụ đề tiếng Việt.")
    text = transcript.read_text(encoding="utf-8")
    if not needs_spoken_rewrite(text):
        return None
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding="utf-8")
    return source


def write_spoken_progress(chunks: list[str], out_dir: Path, merged: Path) -> None:
    """Ghép đoạn đã viết lại với phần gốc còn lại, để bản nghe không bị cụt."""
    parts = []
    for n, chunk in enumerate(chunks):
        path = out_dir / f"{n:04d}.txt"
        if path.exists() and path.stat().st_size > 0:
            parts.append(path.read_text(encoding="utf-8").strip())
        else:
            parts.append(chunk.strip())
    body = "\n\n".join(p for p in parts if p) + "\n"
    merged.parent.mkdir(parents=True, exist_ok=True)
    tmp = merged.with_suffix(".txt.tmp")
    tmp.write_text(body, encoding="utf-8")
    for attempt in range(8):
        try:
            tmp.replace(merged)
            return
        except PermissionError:
            if attempt == 7:
                print(f"Không ghi đè được {merged.name} vì file đang bị khóa. Giữ các đoạn đã có.", flush=True)
                return
            time.sleep(0.4)


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
    """Dịch en/transcript.txt thành vi/transcript.txt.

    Chunk đã có thì giữ. Nếu đoạn đó quá Hán Việt và chưa được viết lại, chuyển sang tiếng Việt nói rồi mới đi tiếp.
    """
    directory = directory.resolve()
    src = source_transcript(directory)
    glossary_path = directory / "glossary.txt"
    out_dir = chunk_dir(directory)
    merged = translated_transcript(directory)
    if not src.exists():
        raise PipelineError("Chưa có transcript.txt.")

    chosen = resolve_model(provider, model)
    glossary = load_glossary(glossary_path)
    system = SYSTEM_TEMPLATE.format(glossary=glossary)
    spoken = SPOKEN_TEMPLATE.format(glossary=glossary)
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
        stamp = out_dir / f"{n:04d}.style"
        if path.exists() and path.stat().st_size > 0:
            body = path.read_text(encoding="utf-8")
            if not stamp.exists() and too_han_viet(body):
                if limit and done_new >= limit:
                    prev = context_tail(body)
                    finished += 1
                    continue
                report(on_progress, f"Đoạn {n + 1}/{total} quá Hán Việt", finished, total)
                softened = soften_han_viet(
                    translate, spoken, body, prev, max_tokens=8192, on_progress=on_progress
                )
                changed = softened != body
                if changed:
                    path.write_text(softened.strip() + "\n", encoding="utf-8")
                    body = softened
                    done_new += 1
                    merge_files(out_dir, merged, quiet=True)
                stamp.write_text("1", encoding="utf-8")
                if changed and sleep_s:
                    time.sleep(sleep_s)
            elif not stamp.exists():
                stamp.write_text("1", encoding="utf-8")
            prev = context_tail(body)
            finished += 1
            continue
        if limit and done_new >= limit:
            break
        report(on_progress, f"Đang dịch đoạn {n + 1}/{total}", finished, total)
        out = translate_piece(translate, system, chunk, prev, max_tokens=8192, on_progress=on_progress)
        out = soften_han_viet(translate, spoken, out, prev, max_tokens=8192, on_progress=on_progress)
        path.write_text(out.strip() + "\n", encoding="utf-8")
        stamp.write_text("1", encoding="utf-8")
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


def run_spoken_rewrite(
    directory: Path,
    *,
    on_progress=None,
    limit: int = 0,
    chunk_words: int = 1000,
    sleep_s: float = 1.0,
    provider: str = "gemini",
    model: str | None = None,
) -> str:
    """Phụ đề đã là tiếng Việt. Đoạn quá Hán Việt thì viết lại cho dễ nghe.

    Bản gốc nằm ở vi/source.txt. Đoạn chưa viết lại vẫn giữ trong vi/transcript.txt.
    """
    directory = directory.resolve()
    source = ensure_spoken_source(directory)
    merged = translated_transcript(directory)
    if source is None:
        report(on_progress, "Phụ đề tiếng Việt nghe được, không viết lại.")
        return merged.read_text(encoding="utf-8") if merged.exists() else ""

    chosen = resolve_model(provider, model)
    glossary = load_glossary(directory / "glossary.txt")
    spoken = SPOKEN_TEMPLATE.format(glossary=glossary)
    chunks = paragraphs_to_chunks(source.read_text(encoding="utf-8"), chunk_words)
    out_dir = chunk_dir(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    translate = Translator(provider, chosen, load_keys())
    extra = ""
    if provider == "gemini":
        extra = f", {len(translate._gemini_keys)} key"
    total = len(chunks)
    report(on_progress, f"Viết lại Hán Việt — {provider} / {chosen}{extra} — {total} đoạn", 0, total)

    def note(message: str, done: int | None = None, total_n: int | None = None, *, status: str = "running", step: str = "translate") -> None:
        report(on_progress, message, done, total_n)
        if on_progress is None:
            publish_status(
                directory,
                status=status,
                step=step,
                message=message,
                provider=provider,
                mode="vi",
                language="vi",
                error=None,
                **({} if done is None else {"done": done}),
                **({} if total_n is None else {"total": total_n}),
            )

    if on_progress is None:
        publish_status(
            directory,
            status="running",
            step="translate",
            message=f"Viết lại Hán Việt — {total} đoạn",
            done=0,
            total=total,
            provider=provider,
            mode="vi",
            language="vi",
            error=None,
        )

    done_new = 0
    prev = ""
    finished = 0
    for n, chunk in enumerate(chunks):
        path = out_dir / f"{n:04d}.txt"
        stamp = out_dir / f"{n:04d}.style"
        if path.exists() and stamp.exists() and path.stat().st_size > 0:
            prev = context_tail(path.read_text(encoding="utf-8"))
            finished += 1
            continue
        if limit and done_new >= limit:
            break
        if not too_han_viet(chunk):
            kept = apply_alias_glossary(chunk, glossary).strip()
            path.write_text(kept + "\n", encoding="utf-8")
            stamp.write_text("1", encoding="utf-8")
            prev = context_tail(kept)
            finished += 1
            if on_progress is None:
                publish_status(
                    directory,
                    status="running",
                    step="translate",
                    message=f"Viết lại {finished}/{total}",
                    done=finished,
                    total=total,
                    provider=provider,
                    mode="vi",
                    language="vi",
                    error=None,
                )
            continue
        note(f"Đoạn {n + 1}/{total} quá Hán Việt", finished, total)
        softened = apply_alias_glossary(
            soften_han_viet(
                translate, spoken, chunk, prev, max_tokens=8192, on_progress=on_progress, low=0.6
            ),
            glossary,
        )
        path.write_text(softened.strip() + "\n", encoding="utf-8")
        stamp.write_text("1", encoding="utf-8")
        prev = context_tail(softened)
        done_new += 1
        finished += 1
        write_spoken_progress(chunks, out_dir, merged)
        note(f"Viết lại {finished}/{total}", finished, total)
        if sleep_s:
            time.sleep(sleep_s)

    if finished:
        write_spoken_progress(chunks, out_dir, merged)
    if finished >= total:
        note(f"Viết lại xong {finished}/{total}", finished, total, status="done", step="done")
    else:
        note(
            f"Viết lại dở {finished}/{total}. Bấm Bắt đầu để làm tiếp.",
            finished,
            total,
            status="error",
            step="translate",
        )
    return merged.read_text(encoding="utf-8") if merged.exists() else ""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Dịch transcript.txt sang tiếng Việt bằng LLM")
    parser.add_argument("--provider", choices=PROVIDERS, default="gemini")
    parser.add_argument("--model", default=None)
    parser.add_argument("--chunk-words", type=int, default=1000)
    parser.add_argument("--limit", type=int, default=0, help="chỉ dịch hoặc viết lại thêm N đoạn, để thử văn phong")
    parser.add_argument("--sleep", type=float, default=1.0, help="giây nghỉ giữa các request")
    parser.add_argument("--spoken", action="store_true", help="phụ đề đã là tiếng Việt, chỉ viết lại chỗ quá Hán Việt")
    parser.add_argument("--dir", default=".", help="thư mục job (en/transcript.txt, glossary.txt, vi/)")
    args = parser.parse_args()
    run = run_spoken_rewrite if args.spoken else run_translation
    try:
        run(
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
        print("Đã làm một phần. Bỏ --limit để chạy hết.")


if __name__ == "__main__":
    main()
