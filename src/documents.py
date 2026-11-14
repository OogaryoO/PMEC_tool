"""
各資料來源（GitHub、Google Drive）共用的原始文件結構，供 summarizer.py 轉譯為功能卡片。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# 「一個檔案一份文件」的來源：快取鍵用 file_id，檔案更新或移除時舊卡片要清掉
FILE_DOC_TYPES = ("gdrive", "fileshare", "m365")


def source_path_of(doc_type: str, files: Sequence[str], title: str) -> str:
    """權限規則與排除規則比對用的路徑，例如 "gdrive/業務/報價.xlsx"、"fileshare/nas/人資/薪資.xlsx"、"pr/PR #12 ..."。
    卡片（FeatureCard）沒有 metadata，所以只用 doc_type／files／title 計算，文件與卡片結果一致。"""
    if doc_type in FILE_DOC_TYPES and files:
        return f"{doc_type}/{files[0]}"
    return f"{doc_type}/{title}"


@dataclass
class ExtractedDocument:
    """單一來源文件（README / API 規格 / PR / Google Drive 文件 / 共用資料夾檔案）的統一結構。"""

    doc_type: str  # "readme" | "api_spec" | "pr" | "gdrive" | "fileshare" | "m365"
    title: str
    content: str
    files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 來源系統自己的權限（主體字串，見 src/tenant.py）；None＝來源沒有提供
    source_acl: Optional[List[str]] = None

    @property
    def source_path(self) -> str:
        return source_path_of(self.doc_type, self.files, self.title)

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
