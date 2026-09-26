"""
使用 ChromaDB PersistentClient 建立本地向量資料庫，同一個 collection 存放兩種項目
（以 metadata `kind` 區分）：
    - card：summarizer.py 產出的功能卡片 (FeatureCard)，整份文件的概要。
    - chunk：chunker.py 切出的原文段落，承載具體細節（試算表列、文件段落）。

Embedding 策略：
    使用 fastembed 的 ONNX 模型（預設 paraphrase-multilingual-MiniLM-L12-v2，約 220MB，
    支援中文，純 CPU、不需要 torch）。ChromaDB 內建的 all-MiniLM-L6-v2 只訓練英文，
    中文查詢的檢索排名極差，因此不採用。刻意不使用 sentence-transformers：後者會拉入
    GB 等級的 torch 依賴，在 Streamlit Community Cloud 這類 1GB RAM 的免費部署環境會導致
    build 失敗或執行期 OOM。

    collection metadata 記錄建立時的 embedding 模型；設定的模型不同時自動清空重建
    （不同模型的向量不可混用），內容可從功能卡片快取還原或重新同步。
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.config import Settings as ChromaSettings

from src import config
from src.chunker import Chunk
from src.summarizer import FeatureCard

logger = config.get_logger(__name__)

UPSERT_BATCH = 256
# ONNX 推論的批次大小直接決定記憶體峰值：實測 256 → 約 2.5GB、8 → 約 650MB（含模型本身約 610MB），速度相近
EMBED_BATCH = 8


class VectorStoreError(Exception):
    """ChromaDB 初始化或存取失敗時拋出，訊息需可直接呈現給使用者。"""


class FastEmbedFunction(EmbeddingFunction[Documents]):
    """把 fastembed 的 ONNX 模型包成 ChromaDB embedding function。首次使用會下載模型。"""

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding

        self.model_name = model_name
        self._model = TextEmbedding(model_name)

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 - ChromaDB 規定的參數名
        return [vec.tolist() for vec in self._model.embed(list(input), batch_size=EMBED_BATCH)]


def _build_embedding_function(model_name: str) -> FastEmbedFunction:
    try:
        return FastEmbedFunction(model_name)
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"無法載入 Embedding 模型 {model_name}：{exc}") from exc


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
        embedding_model: Optional[str] = None,
    ) -> None:
        self.persist_dir = persist_dir or config.CHROMA_PERSIST_DIR
        self.collection_name = collection_name or config.CHROMA_COLLECTION_NAME
        self.embedding_model = embedding_model or config.EMBEDDING_MODEL

        try:
            self._client = chromadb.PersistentClient(
                path=self.persist_dir,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                f"初始化 ChromaDB 失敗（路徑：{self.persist_dir}）：{exc}"
            ) from exc

        self._embedding_function = _build_embedding_function(self.embedding_model)

        # hnsw:search_ef 預設 10：原文段落上千筆時近似搜尋會漏掉真正最相近的結果
        # （實測精確排名第 1 的卡片不在 HNSW 前 60 名內），調高至接近精確搜尋。
        desired_metadata = {
            "hnsw:space": "cosine",
            "hnsw:construction_ef": 200,
            "hnsw:search_ef": 200,
            "embedding_model": self.embedding_model,
        }
        try:
            existing = {c.name: c for c in self._client.list_collections()}
            old = existing.get(self.collection_name)
            if old is not None and (old.metadata or {}) != desired_metadata:
                logger.warning(
                    "Collection '%s' 的 embedding 模型或索引設定不同（%s），清空後依目前設定重建。",
                    self.collection_name,
                    old.metadata,
                )
                self._client.delete_collection(self.collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self._embedding_function,
                metadata=desired_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"建立/取得 Collection '{self.collection_name}' 失敗：{exc}") from exc

    # ------------------------------------------------------------------
    def _upsert(self, ids: List[str], documents: List[str], metadatas: List[Dict]) -> None:
        try:
            for start in range(0, len(ids), UPSERT_BATCH):
                end = start + UPSERT_BATCH
                self._collection.upsert(
                    ids=ids[start:end], documents=documents[start:end], metadatas=metadatas[start:end]
                )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"寫入向量資料庫失敗：{exc}") from exc

    def upsert_cards(self, cards: List[FeatureCard]) -> int:
        """將功能卡片寫入向量資料庫（依內容雜湊 upsert，避免重複）。"""
        if not cards:
            return 0
        self._upsert(
            ids=[_card_id(c) for c in cards],
            documents=[c.to_embedding_text() for c in cards],
            metadatas=[{**c.to_metadata(), "kind": "card"} for c in cards],
        )
        logger.info("已寫入/更新 %d 筆功能卡片至向量資料庫。", len(cards))
        return len(cards)

    def replace_chunks(self, chunks: List[Chunk]) -> int:
        """以本次抽取的原文段落取代資料庫中所有既有段落（來源移除或內容變動後不殘留舊段落）。"""
        try:
            self._collection.delete(where={"kind": "chunk"})
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"刪除舊的原文段落失敗：{exc}") from exc
        # 同一份文件內重複的段落（例如試算表的相同列）id 相同，upsert 單批內不可重複
        unique = list({c.chunk_id: c for c in chunks}.values())
        if unique:
            self._upsert(
                ids=[c.chunk_id for c in unique],
                documents=[c.to_embedding_text() for c in unique],
                metadatas=[c.to_metadata() for c in unique],
            )
        logger.info("已寫入 %d 筆原文段落至向量資料庫。", len(unique))
        return len(unique)

    def query(self, query_text: str, top_k: int = 8) -> List[Dict]:
        """相似度檢索（卡片與原文段落混合），回傳依相似度排序的結果列表。"""
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

    def count(self, kind: Optional[str] = None) -> int:
        """資料庫筆數；kind 為 "card" / "chunk" 時只計算該類項目。"""
        try:
            if kind is None:
                return self._collection.count()
            return len(self._collection.get(where={"kind": kind}, include=[])["ids"])
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"讀取向量資料庫筆數失敗：{exc}") from exc

    def delete_by_doc_type(self, doc_type: str) -> None:
        """刪除指定來源類型的所有項目（例如同步前清掉舊的 Drive 卡片，再以快取內容重建）。"""
        try:
            self._collection.delete(where={"doc_type": doc_type})
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"刪除向量資料庫中 {doc_type} 資料失敗：{exc}") from exc
