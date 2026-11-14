"""
功能卡片摘要快取：把 Summarizer 產出的 FeatureCard 存在 PostgreSQL `feature_cards` 表，
讓 sync_pipeline 只對「快取中不存在」的文件呼叫 RAP LLM，避免重複消耗額度。

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
from typing import Dict, List, Set

from sqlalchemy import Engine, text

from src import config
from src.documents import FILE_DOC_TYPES, ExtractedDocument

logger = config.get_logger(__name__)


class CardCacheError(Exception):
    """讀寫快取失敗時拋出，訊息需可直接呈現給使用者。"""


def doc_identity(doc: ExtractedDocument) -> str:
    """產生文件的穩定識別鍵，用來判斷是否需要重新呼叫 RAP 摘要。"""
    if doc.doc_type == "pr":
        number = doc.metadata.get("number")
        return f"pr:{number}"
    content_hash = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()[:16]
    if doc.doc_type in FILE_DOC_TYPES:
        # file_id 只含英數、`-`、`_`（Drive ID；共用資料夾用路徑雜湊），不會與分隔用的冒號衝突
        return f"{doc.doc_type}:{doc.metadata['file_id']}:{content_hash}"
    path = doc.files[0] if doc.files else doc.title
    return f"{doc.doc_type}:{path}:{content_hash}"


def drop_file_entries(cache: Dict[str, dict], doc_type: str, file_ids: Set[str]) -> List[str]:
    """無條件刪除指定檔案的所有卡片（用於被排除規則命中的敏感檔案），回傳被刪除的快取鍵。"""
    removed = []
    for key in list(cache):
        prefix, _, rest = key.partition(":")
        if prefix == doc_type and rest.split(":", 1)[0] in file_ids:
            del cache[key]
            removed.append(key)
    return removed


def prune_gdrive_entries(
    cache: Dict[str, dict],
    current_docs: List[ExtractedDocument],
    seen_file_ids: Set[str],
    listing_complete: bool,
) -> List[str]:
    """Drive 專用的舊介面，等同 prune_file_entries(..., doc_type="gdrive")。"""
    return prune_file_entries(cache, current_docs, seen_file_ids, listing_complete, doc_type="gdrive")


def prune_file_entries(
    cache: Dict[str, dict],
    current_docs: List[ExtractedDocument],
    seen_file_ids: Set[str],
    listing_complete: bool,
    doc_type: str = "gdrive",
) -> List[str]:
    """就地刪除過期的檔案卡片（Drive、共用資料夾…），回傳被刪除的快取鍵。

    對每個 `<doc_type>:` 開頭的快取鍵：
        1. 是該檔案本次內容的鍵 → 保留。
        2. 該檔案本次有新內容（舊版本）→ 新版本已成功摘要進快取才刪除；
           新版摘要失敗則保留舊卡片，下次同步重試。
        3. 本次有掃描到該檔案但沒有內容（讀取失敗／無文字／過大）→ 保留。
        4. 其他（檔案已不在同步資料夾中）→ 只有掃描完整時才刪除，
           避免權限暫時異常時誤刪、之後又得花 RAP 額度重新摘要。
    其他 doc_type 的鍵一律不動。
    """
    current_key_by_id = {
        d.metadata["file_id"]: doc_identity(d) for d in current_docs if d.doc_type == doc_type
    }
    removed: List[str] = []
    for key in list(cache):
        if not key.startswith(f"{doc_type}:"):
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


def load_cards(engine: Engine) -> Dict[str, dict]:
    """讀取快取，回傳 {doc_identity: FeatureCard 欄位字典}。"""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT cache_key, card FROM feature_cards")).all()
    except Exception as exc:  # noqa: BLE001
        raise CardCacheError(f"讀取功能卡片快取失敗：{exc}") from exc
    return {r.cache_key: r.card for r in rows}


def save_cards(engine: Engine, cache: Dict[str, dict]) -> None:
    """讓資料庫中的快取恰好等於 cache：新增缺少的鍵、刪除已移除的鍵（既有鍵的內容不變）。"""
    try:
        with engine.begin() as conn:
            existing = set(conn.execute(text("SELECT cache_key FROM feature_cards")).scalars())
            removed = [k for k in existing if k not in cache]
            added = [k for k in cache if k not in existing]
            if removed:
                conn.execute(text("DELETE FROM feature_cards WHERE cache_key = ANY(:keys)"), {"keys": removed})
            if added:
                conn.execute(
                    text("INSERT INTO feature_cards (cache_key, card) VALUES (:k, CAST(:c AS jsonb))"),
                    [{"k": k, "c": json.dumps(cache[k], ensure_ascii=False)} for k in added],
                )
    except Exception as exc:  # noqa: BLE001
        raise CardCacheError(f"寫入功能卡片快取失敗：{exc}") from exc
    logger.info("功能卡片快取：新增 %d 筆、移除 %d 筆，共 %d 筆。", len(added), len(removed), len(cache))
