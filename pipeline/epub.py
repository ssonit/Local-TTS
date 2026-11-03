"""Đọc chữ từ file EPUB để đưa vào cùng quy trình với phụ đề YouTube."""

from __future__ import annotations

import base64
import hashlib
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import unquote
from xml.etree import ElementTree

from .errors import PipelineError

_BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "tr"}
_SKIP = {"script", "style", "head"}
_HTML = {"application/xhtml+xml", "text/html", "application/html+xml"}
_VI = re.compile(
    "["
    "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợ"
    "ùúủũụưừứửữựỳýỷỹỵđ"
    "ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢ"
    "ÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ"
    "]"
)
_DOT = re.compile(r"^[.\u2026…\s]+$")


@dataclass(frozen=True)
class EpubBook:
    title: str
    creator: str
    language: str
    text: str
    chapters: int


def job_id_for(data: bytes) -> str:
    token = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii")
    job_id = token[:11]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", job_id):
        raise PipelineError("Không tạo được id cho EPUB")
    return job_id


def sniff_language(text: str) -> str:
    sample = text[:50000]
    letters = re.findall(r"[A-Za-zÀ-ỹ]", sample)
    if not letters:
        return "en"
    if len(_VI.findall(sample)) / len(letters) >= 0.015:
        return "vi"
    return "en"


def extract_epub(data: bytes) -> EpubBook:
    if not data.startswith(b"PK"):
        raise PipelineError("File không phải EPUB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PipelineError("Không đọc được file EPUB") from exc
    with archive:
        opf_path = _opf_path(archive)
        try:
            root = ElementTree.fromstring(archive.read(opf_path))
        except ElementTree.ParseError as exc:
            raise PipelineError("Không đọc được mục lục EPUB") from exc
        title = _meta(root, "title") or "EPUB"
        creator = _meta(root, "creator")
        documents = _chapters(archive, opf_path, root)
    parts: list[str] = []
    kept = 0
    for html in documents:
        paragraphs = _html_to_paragraphs(html)
        if not paragraphs:
            continue
        kept += 1
        parts.extend(paragraphs)
    text = "\n\n".join(parts).strip()
    if not text:
        raise PipelineError("EPUB không có chữ")
    return EpubBook(
        title=title.strip() or "EPUB",
        creator=creator.strip(),
        language=sniff_language(text),
        text=text,
        chapters=kept,
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _meta(root: ElementTree.Element, name: str) -> str:
    for el in root.iter():
        if _local(el.tag) == name and el.text:
            return el.text.strip()
    return ""


def _opf_path(archive: zipfile.ZipFile) -> str:
    try:
        raw = archive.read("META-INF/container.xml")
    except KeyError:
        raw = b""
    if raw:
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise PipelineError("Không đọc được mục lục EPUB") from exc
        for el in root.iter():
            if _local(el.tag) == "rootfile" and el.attrib.get("full-path"):
                return el.attrib["full-path"]
    for name in archive.namelist():
        if name.lower().endswith(".opf"):
            return name
    raise PipelineError("EPUB không có mục lục")


def _chapters(archive: zipfile.ZipFile, opf_path: str, root: ElementTree.Element) -> list[str]:
    manifest: dict[str, ElementTree.Element] = {}
    for el in root.iter():
        if _local(el.tag) != "item":
            continue
        item_id = el.attrib.get("id")
        if item_id and el.attrib.get("href"):
            manifest[item_id] = el
    opf_dir = posixpath.dirname(opf_path)
    documents: list[str] = []
    for el in root.iter():
        if _local(el.tag) != "itemref":
            continue
        if el.attrib.get("linear") == "no":
            continue
        item = manifest.get(el.attrib.get("idref", ""))
        if item is None or _is_nav(item) or not _is_html(item):
            continue
        path = _zip_path(opf_dir, item.attrib["href"])
        try:
            raw = archive.read(path)
        except KeyError:
            continue
        documents.append(raw.decode("utf-8", "replace"))
    return documents


def _is_nav(item: ElementTree.Element) -> bool:
    return "nav" in item.attrib.get("properties", "").split()


def _is_html(item: ElementTree.Element) -> bool:
    media = item.attrib.get("media-type", "")
    if media in _HTML:
        return True
    href = item.attrib.get("href", "").lower()
    return href.endswith((".xhtml", ".html", ".htm"))


def _zip_path(opf_dir: str, href: str) -> str:
    href = unquote(href.split("#", 1)[0])
    path = posixpath.normpath(posixpath.join(opf_dir, href))
    if path.startswith("../") or path.startswith("/"):
        raise PipelineError("Đường dẫn trong EPUB không hợp lệ")
    return path


def _html_to_paragraphs(html: str) -> list[str]:
    parser = _TextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    return parser.parts


class _TextParser:
    def __init__(self) -> None:
        from html.parser import HTMLParser

        self._parser = HTMLParser(convert_charrefs=True)
        self._parser.handle_starttag = self._start
        self._parser.handle_endtag = self._end
        self._parser.handle_data = self._data
        self._skip = 0
        self._buf: list[str] = []
        self.parts: list[str] = []

    def feed(self, html: str) -> None:
        self._parser.feed(html)

    def close(self) -> None:
        self._parser.close()
        self._flush()

    def _start(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self._skip += 1
            return
        if tag == "br":
            self._buf.append(" ")
        if tag in _BLOCK:
            self._flush()

    def _end(self, tag: str) -> None:
        if tag in _SKIP and self._skip:
            self._skip -= 1
            return
        if tag in _BLOCK:
            self._flush()

    def _data(self, data: str) -> None:
        if self._skip:
            return
        self._buf.append(data)

    def _flush(self) -> None:
        text = "".join(self._buf).replace("\u00a0", " ")
        text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        self._buf = []
        if text and not _DOT.fullmatch(text):
            self.parts.append(text)
