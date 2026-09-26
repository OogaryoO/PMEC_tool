"""
各資料來源（GitHub、Google Drive）共用的原始文件結構，供 summarizer.py 轉譯為功能卡片。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ExtractedDocument:
    """單一來源文件（README / API 規格 / PR / Google Drive 文件）的統一結構。"""

    doc_type: str  # "readme" | "api_spec" | "pr" | "gdrive"
    title: str
    content: str
    files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_llm_text(self, max_chars: int = 6000) -> str:
        """轉成適合丟給 LLM 的純文字區塊（供 summarizer 使用）。"""
        files_part = ""
        if self.files:
            shown = self.files[:30]
            more = f"\n...(共 {len(self.files)} 個檔案，僅顯示前 {len(shown)} 個)" if len(self.files) > 30 else ""
            files_part = "\n\n變更/相關檔案：\n- " + "\n- ".join(shown) + more

        meta_lines = [f"{k}: {v}" for k, v in self.metadata.items() if v not in (None, "", [])]
        meta_part = ("\n" + "\n".join(meta_lines)) if meta_lines else ""

        body = self.content.strip()
        if len(body) > max_chars:
            body = body[:max_chars] + "\n...(內容過長，已截斷)"

        return (
            f"[來源類型] {self.doc_type}\n"
            f"[標題] {self.title}"
            f"{meta_part}\n"
            f"[內容]\n{body}"
            f"{files_part}"
        )
