"""Bounded extraction and stable source locators. No OCR or remote fetching."""

import io
import re
from pathlib import Path

from pypdf import PdfReader

from .config import Settings
from .schemas import SourceChunk
from .unicode_utils import normalize_unicode


class SourceError(ValueError):
    pass


def split_sentences(text: str, max_chars: int = 220) -> list[str]:
    """Short utterances also work for Chinese text without whitespace."""
    pieces = re.split(r"(?<=[。！？!?；;])\s*|(?<=[.])\s+|\n+", text)
    sentences: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        for start in range(0, len(piece), max_chars):
            sentences.append(piece[start:start + max_chars])
    return sentences


def text_chunks(text: str, locator_prefix: str = "原文", default_title: str = "讲解") -> list[SourceChunk]:
    # Normalize line endings without stripping the document: line numbers must
    # still refer to the user's original file, including leading blank lines.
    text = normalize_unicode(text).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise SourceError("没有可读取的正文。")
    chunks: list[SourceChunk] = []
    title = default_title
    pending: list[tuple[int, str]] = []
    pending_chars = 0

    def flush() -> None:
        nonlocal pending, pending_chars
        nonempty = [index for index, (_, part) in enumerate(pending) if part.strip()]
        if nonempty:
            content = pending[nonempty[0]:nonempty[-1] + 1]
            body = "\n".join(part for _, part in content).strip()
            chunks.append(SourceChunk(title=title[:160], text=body,
                                      locator=f"{locator_prefix} · 行 {content[0][0]}–{content[-1][0]}"))
        pending = []
        pending_chars = 0

    for line_no, line in enumerate(text.splitlines(), start=1):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+)$", line)
        if heading:
            flush()
            title = heading.group(1).strip()
            continue
        # Long unbroken paragraphs must still produce bounded chunks.
        parts = [line[i:i + 1800] for i in range(0, len(line), 1800)] or [""]
        for part in parts:
            if pending and pending_chars + 1 + len(part) > 1800:
                flush()
            if not pending and not part.strip():
                continue
            pending_chars += len(part) + (1 if pending else 0)
            pending.append((line_no, part))
    flush()
    if not chunks:
        raise SourceError("正文为空；请在标题后加入教材内容。")
    if len(chunks) > 80:
        raise SourceError("章节过多；请先导入不超过 80 个小节的一章教材。")
    return chunks


def extract_upload(filename: str, data: bytes, settings: Settings) -> list[SourceChunk]:
    suffix = Path(filename).suffix.lower()
    if len(data) > settings.max_upload_bytes:
        raise SourceError("文件超过 8 MB，请拆分后再导入。")
    if suffix in {".txt", ".md"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceError("TXT/Markdown 请使用 UTF-8 编码保存。") from exc
        if len(text) > settings.max_source_chars:
            raise SourceError("正文超过 60000 字，请按章节导入。")
        return text_chunks(text)
    if suffix != ".pdf":
        raise SourceError("仅支持 .txt、.md 和带文字层的 .pdf。")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise SourceError("暂不支持加密 PDF，请先解除密码保护。")
        if len(reader.pages) > settings.max_pdf_pages:
            raise SourceError("PDF 超过 30 页，请按章节拆分。")
        chunks: list[SourceChunk] = []
        char_count = 0
        for page_no, page in enumerate(reader.pages, 1):
            text = normalize_unicode(page.extract_text() or "").strip()
            char_count += len(text)
            if char_count > settings.max_source_chars:
                raise SourceError("PDF 文字超过 60000 字，请拆分后导入。")
            if text:
                chunks.extend(text_chunks(text, f"PDF 第 {page_no} 页", f"第 {page_no} 页"))
        if not chunks:
            raise SourceError("PDF 没有可提取的文字，可能是扫描件；首版暂不支持 OCR。")
        if len(chunks) > 80:
            raise SourceError("PDF 内容过长，请导入更短的章节。")
        return chunks
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("PDF 无法读取，请确认文件有效且包含文字层。") from exc
