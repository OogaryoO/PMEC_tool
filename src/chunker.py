"""
把 ExtractedDocument 的原文切成可檢索的段落 (Chunk)，與功能卡片一起寫入向量資料庫。

功能卡片只涵蓋文件前段的摘要；具體細節（試算表的某一列、PDF 的某一段）要靠原文段落檢索。
切段規則：
    - 以行為單位累積，約 CHUNK_CHARS 字切一段；段落大小受 embedding 模型輸入上限
      （paraphrase-multilingual-MiniLM-L12-v2 為 128 token，約 300 個中文字）限制，
      過長的內容模型會直接截斷、後段不會被檢索到。
    - `# ` ~ `###### ` 開頭的行（Markdown 標題、試算表的「## 工作表：名稱」）視為區段標題，
      不併入內文，而是附在其後每一段的前綴，讓段落單獨被檢索時仍保有上下文。
    - 單行超過 CHUNK_CHARS 時優先在「｜」欄位邊界切開（試算表列），單一欄位仍過長才硬切；
      續段以該行第一個欄位作前綴（試算表列的第一欄通常是識別欄，例如「Phase：P3」）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Iterator, List

from src.documents import ExtractedDocument

CHUNK_CHARS = 300
CONTINUATION_PREFIX_CHARS = 40
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")


@dataclass
class Chunk:
    doc_type: str
    source_title: str
    source_ref: str
    section: str
    text: str
    index: int

    @property
    def chunk_id(self) -> str:
        key = f"chunk::{self.doc_type}::{self.source_ref}::{self.source_title}::{self.index}::{self.text}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    def to_embedding_text(self) -> str:
        header = self.source_title + (f"｜{self.section}" if self.section else "")
        return f"{header}\n{self.text}"

    def to_metadata(self) -> Dict[str, str]:
        return {
            "kind": "chunk",
            "doc_type": self.doc_type,
            "source_title": self.source_title,
            "source_ref": self.source_ref,
            "section": self.section,
        }


def _hard_split(text: str, limit: int) -> Iterator[str]:
    for start in range(0, len(text), limit):
        yield text[start : start + limit]


def _pieces(line: str) -> Iterator[str]:
    """把單行切成不超過 CHUNK_CHARS 的片段；續段帶上原行第一個欄位作為前綴。"""
    if len(line) <= CHUNK_CHARS:
        yield line
        return
    fields = line.split("｜")
    prefix = fields[0][:CONTINUATION_PREFIX_CHARS] + "…（續）｜"
    limit = CHUNK_CHARS - len(prefix)
    current = ""
    first = True
    for field_text in fields:
        for part in _hard_split(field_text, limit):
            candidate = f"{current}｜{part}" if current else part
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                yield current if first else prefix + current
                first = False
            current = part
    if current:
        yield current if first else prefix + current


def source_ref_of(doc: ExtractedDocument) -> str:
    """與 Summarizer 產生卡片時相同的來源參照：PR / Drive 用網址，其餘用檔案路徑。"""
    if doc.doc_type in ("pr", "gdrive"):
        return str(doc.metadata.get("url") or (doc.files[0] if doc.files else ""))
    return doc.files[0] if doc.files else ""


def chunk_document(doc: ExtractedDocument) -> List[Chunk]:
    source_ref = source_ref_of(doc)
    chunks: List[Chunk] = []
    section = ""
    buf: List[str] = []
    size = 0
    in_code = False  # 程式碼區塊內的「# 註解」不是標題

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append(
                Chunk(
                    doc_type=doc.doc_type,
                    source_title=doc.title,
                    source_ref=source_ref,
                    section=section,
                    text="\n".join(buf),
                    index=len(chunks),
                )
            )
        buf, size = [], 0

    for raw in doc.content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("```"):
            in_code = not in_code
        heading = None if in_code else _HEADING_RE.match(line)
        if heading:
            flush()
            section = heading.group(1).strip()
            continue
        for piece in _pieces(line):
            if size and size + len(piece) + 1 > CHUNK_CHARS:
                flush()
            buf.append(piece)
            size += len(piece) + 1
    flush()
    return chunks
