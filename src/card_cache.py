"""
功能卡片摘要快取：把 Summarizer 產出的 FeatureCard 以 JSON 檔案形式寫回一個
GitHub repo，作為跨部署重啟的持久化儲存。

為什麼需要這個：
    Streamlit Community Cloud 的檔案系統是 ephemeral，app 休眠或重新部署後，
    本地的 ChromaDB (`chroma_data/`) 會被整個清空。若同步流程每次都對「所有」
    抓到的文件重新呼叫 RAP LLM 摘要，會不必要地重複消耗國網 AI RAP 額度。

    這個快取把「已經摘要過的結果」持久化在 GitHub repo 裡（免費，PyGithub
    讀寫 API 不用額外花錢），讓 sync_pipeline 只對「快取中不存在」的文件才
    呼叫 RAP LLM，並可用快取內容免費、隨時重建 ChromaDB。

快取鍵 (doc_identity)：
    - PR：一旦 merge 內容不會再變，用 `pr:<number>` 當作永久鍵。
    - README / API 規格：內容可能隨時間變動，用
      `<doc_type>:<path>:<內容雜湊>`，內容一變就視為新文件，重新摘要。
    - Google Drive：`gdrive:<file_id>:<內容雜湊>`；同一檔案有新版本卡片後舊版本
      即移除，檔案移出同步資料夾時（且掃描完整）亦移除。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from github import Auth, Github
from github.GithubException import GithubException, UnknownObjectException

from src import config
from src.documents import ExtractedDocument

logger = config.get_logger(__name__)


class CardCacheError(Exception):
    """讀寫快取檔案失敗時拋出，訊息需可直接呈現給使用者。"""


def doc_identity(doc: ExtractedDocument) -> str:
    """產生文件的穩定識別鍵，用來判斷是否需要重新呼叫 RAP 摘要。"""
    if doc.doc_type == "pr":
        number = doc.metadata.get("number")
        return f"pr:{number}"
    content_hash = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()[:16]
    if doc.doc_type == "gdrive":
        # Drive 檔案 ID 只含英數、`-`、`_`，不會與分隔用的冒號衝突
        return f"gdrive:{doc.metadata['file_id']}:{content_hash}"
    path = doc.files[0] if doc.files else doc.title
    return f"{doc.doc_type}:{path}:{content_hash}"


def prune_gdrive_entries(
    cache: Dict[str, dict],
    current_docs: List[ExtractedDocument],
    seen_file_ids: Set[str],
    listing_complete: bool,
) -> List[str]:
    """就地刪除過期的 Drive 卡片，回傳被刪除的快取鍵。

    對每個 `gdrive:` 開頭的快取鍵：
        1. 是該檔案本次內容的鍵 → 保留。
        2. 該檔案本次有新內容（舊版本）→ 新版本已成功摘要進快取才刪除；
           新版摘要失敗則保留舊卡片，下次同步重試。
        3. 本次有掃描到該檔案但沒有內容（讀取失敗／無文字／過大）→ 保留。
        4. 其他（檔案已不在同步資料夾中）→ 只有掃描完整時才刪除，
           避免權限暫時異常時誤刪、之後又得花 RAP 額度重新摘要。
    非 `gdrive:` 開頭的鍵一律不動。
    """
    current_key_by_id = {
        d.metadata["file_id"]: doc_identity(d) for d in current_docs if d.doc_type == "gdrive"
    }
    removed: List[str] = []
    for key in list(cache):
        if not key.startswith("gdrive:"):
            continue
        file_id = key.split(":", 2)[1]
        current_key = current_key_by_id.get(file_id)
        if key == current_key:
            continue
        if current_key is not None:
            stale = current_key in cache
        elif file_id in seen_file_ids:
            stale = False
        else:
            stale = listing_complete
        if stale:
            del cache[key]
            removed.append(key)
    return removed


@dataclass
class CardCache:
    """封裝以 GitHub repo 檔案作為儲存後端的功能卡片快取（讀取／寫回）。"""

    repo_full_name: str
    file_path: str
    token: str
    branch: str = ""
    _sha: Optional[str] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.token:
            raise CardCacheError("GITHUB_TOKEN 未設定，無法讀寫功能卡片快取檔案。")
        if not self.repo_full_name or "/" not in self.repo_full_name:
            raise CardCacheError(f"CACHE_REPO 格式錯誤：'{self.repo_full_name}'，需為 `owner/repo`。")
        try:
            self._client = Github(auth=Auth.Token(self.token), timeout=30)
            self._repo = self._client.get_repo(self.repo_full_name)
        except GithubException as exc:
            raise CardCacheError(f"連線快取倉庫 `{self.repo_full_name}` 失敗：{exc}") from exc

    def load(self) -> Dict[str, dict]:
        """讀取快取檔案，回傳 {doc_identity: FeatureCard 欄位字典}；不存在則回傳空字典。"""
        try:
            kwargs = {"ref": self.branch} if self.branch else {}
            content_file = self._repo.get_contents(self.file_path, **kwargs)
            if isinstance(content_file, list):
                raise CardCacheError(f"快取路徑 '{self.file_path}' 是目錄，不是檔案。")
            self._sha = content_file.sha
            raw = content_file.decoded_content.decode("utf-8")
        except UnknownObjectException:
            self._sha = None
            return {}
        except GithubException as exc:
            raise CardCacheError(f"讀取快取檔案失敗：{exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("快取檔案內容非合法 JSON，視為空快取：%s", exc)
            return {}

        if not isinstance(data, dict):
            logger.warning("快取檔案格式非預期（非 JSON object），視為空快取。")
            return {}
        return data

    def save(self, cache: Dict[str, dict]) -> None:
        """把最新的快取內容寫回 GitHub（檔案不存在則建立，存在則更新）。"""
        body = json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True)
        kwargs = {"branch": self.branch} if self.branch else {}
        try:
            if self._sha:
                self._repo.update_file(
                    self.file_path, "chore: 更新 PMEC 功能卡片快取", body, self._sha, **kwargs
                )
            else:
                self._repo.create_file(
                    self.file_path, "chore: 建立 PMEC 功能卡片快取", body, **kwargs
                )
        except GithubException as exc:
            raise CardCacheError(f"寫入快取檔案失敗：{exc}") from exc
