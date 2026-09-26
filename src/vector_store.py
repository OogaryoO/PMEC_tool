"""
使用 ChromaDB PersistentClient 建立本地向量資料庫，儲存與檢索
summarizer.py 產出的「功能卡片」(FeatureCard)。

Embedding 策略：
    固定使用 ChromaDB 內建的 DefaultEmbeddingFunction（ONNX Runtime，
    all-MiniLM-L6-v2，約數十 MB，純 CPU 執行、免費、不需要 torch）。
    刻意不使用 sentence-transformers：後者會拉入 GB 等級的 torch 依賴，
    在 Streamlit Community Cloud 這類 1GB RAM 的免費部署環境會導致
    build 失敗或執行期 OOM。
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from src import config
from src.summarizer import FeatureCard

logger = config.get_logger(__name__)


class VectorStoreError(Exception):
    """ChromaDB 初始化或存取失敗時拋出，訊息需可直接呈現給使用者。"""


def _build_embedding_function():
    """建立 ChromaDB 內建的輕量 embedding function（免費、無需下載 GB 級套件）。"""
    try:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        return DefaultEmbeddingFunction()
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"無法初始化 ChromaDB 內建 Embedding 函式：{exc}") from exc


def _card_id(card: FeatureCard) -> str:
    """依內容產生穩定 id，讓重複同步時可以 upsert 而不是無限堆疊重複資料。"""
    key = f"{card.doc_type}::{card.source_ref}::{card.source_title}::{card.feature_name}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


class VectorStore:
    """封裝 ChromaDB PersistentClient 的寫入與檢索邏輯。"""

    def __init__(
        self,
        persist_dir: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> None:
        self.persist_dir = persist_dir or config.CHROMA_PERSIST_DIR
        self.collection_name = collection_name or config.CHROMA_COLLECTION_NAME

        try:
            self._client = chromadb.PersistentClient(
                path=self.persist_dir,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                f"初始化 ChromaDB 失敗（路徑：{self.persist_dir}）：{exc}"
            ) from exc

        self._embedding_function = _build_embedding_function()

        try:
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self._embedding_function,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"建立/取得 Collection '{self.collection_name}' 失敗：{exc}") from exc

    # ------------------------------------------------------------------
    def upsert_cards(self, cards: List[FeatureCard]) -> int:
        """將功能卡片寫入向量資料庫（依內容雜湊 upsert，避免重複）。"""
        if not cards:
            return 0

        ids = [_card_id(c) for c in cards]
        documents = [c.to_embedding_text() for c in cards]
        metadatas = [c.to_metadata() for c in cards]

        try:
            self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"寫入向量資料庫失敗：{exc}") from exc

        logger.info("已寫入/更新 %d 筆功能卡片至向量資料庫。", len(cards))
        return len(cards)

    def query(self, query_text: str, top_k: int = 4) -> List[Dict]:
        """相似度檢索，回傳依相似度排序的結果列表。"""
        if not query_text or not query_text.strip():
            raise VectorStoreError("查詢文字不可為空。")

        try:
            count = self._collection.count()
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"讀取向量資料庫失敗：{exc}") from exc

        if count == 0:
            return []

        try:
            result = self._collection.query(
                query_texts=[query_text],
                n_results=min(top_k, count),
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"向量檢索失敗：{exc}") from exc

        hits: List[Dict] = []
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]

        for i in range(len(ids)):
            hits.append(
                {
                    "id": ids[i],
                    "document": documents[i],
                    "metadata": metadatas[i] or {},
                    "distance": distances[i] if i < len(distances) else None,
                }
            )
        return hits

    def count(self) -> int:
        try:
            return self._collection.count()
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"讀取向量資料庫筆數失敗：{exc}") from exc

    def delete_by_doc_type(self, doc_type: str) -> None:
        """刪除指定來源類型的所有卡片（例如同步前清掉舊的 Drive 卡片，再以快取內容重建）。"""
        try:
            self._collection.delete(where={"doc_type": doc_type})
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"刪除向量資料庫中 {doc_type} 卡片失敗：{exc}") from exc

    def reset(self) -> None:
        """清空整個 collection（完整重新同步前使用）。"""
        try:
            self._client.delete_collection(self.collection_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("刪除舊 collection 時發生警告（可能原本就不存在）：%s", exc)
        try:
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self._embedding_function,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"重建 Collection 失敗：{exc}") from exc
