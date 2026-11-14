"""
PostgreSQL + pgvector 檢索索引。同一張表 `retrieval_items`
存放兩種項目（以 `kind` 區分）：
    - card：summarizer.py 產出的功能卡片 (FeatureCard)，整份文件的概要。
    - chunk：chunker.py 切出的原文段落，承載具體細節（試算表列、文件段落）。

Embedding 策略：
    使用 fastembed 的 ONNX 模型（預設 paraphrase-multilingual-MiniLM-L12-v2，支援中文、
    純 CPU、不需要 torch），在本服務內計算，文字不送往第三方。

同步採「差異更新」：項目 id 由內容決定，只對新出現的項目計算 embedding、刪除已不存在的項目，
不必每次同步重算全部向量。

檢索不建 ANN 索引、直接精確計算距離：資料量在數千段等級時精確掃描夠快，也避免近似搜尋漏掉
真正最相近的結果。

權限：每筆項目有 acl（主體字串陣列，見 src/tenant.py）。檢索時「先過濾、再排序」，
使用者看不到的項目不會進入回答。acl 改變不必重算向量，只更新該欄位。
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import Engine, text

from src import config
from src.chunker import Chunk
from src.summarizer import FeatureCard

logger = config.get_logger(__name__)

# ONNX 推論的批次大小直接決定記憶體峰值：實測 256 → 約 2.5GB、8 → 約 650MB（含模型本身約 610MB），速度相近
EMBED_BATCH = 8
WRITE_BATCH = 256


class IndexStoreError(Exception):
    """檢索索引存取失敗時拋出，訊息需可直接呈現給使用者。"""


class Embedder:
    """把 fastembed 的 ONNX 模型包成「文字 → 向量」函式。首次使用會下載（或讀取映像內預載的）模型。"""

    def __init__(self, model_name: str) -> None:
        try:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name)
        except Exception as exc:  # noqa: BLE001
            raise IndexStoreError(f"無法載入 Embedding 模型 {model_name}：{exc}") from exc
        self.model_name = model_name

    def __call__(self, texts: Sequence[str]) -> List[List[float]]:
        return [vec.tolist() for vec in self._model.embed(list(texts), batch_size=EMBED_BATCH)]


def _card_id(card: FeatureCard) -> str:
    """依卡片來源與主題產生穩定 id，重複同步時覆寫同一筆而不是堆疊重複資料。"""
    key = f"{card.doc_type}::{card.source_ref}::{card.source_title}::{card.feature_name}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


# (id, kind, doc_type, content, metadata, acl)
_Item = Tuple[str, str, str, str, Dict, List[str]]
EVERYONE_ACL = ["*"]


def _norm_acl(acl: Optional[Sequence[str]]) -> List[str]:
    return sorted(set(acl)) if acl else list(EVERYONE_ACL)


class IndexStore:
    """封裝 retrieval_items 的寫入與檢索。"""

    def __init__(self, engine: Engine, embedder: Optional[Embedder] = None) -> None:
        self._engine = engine
        self._embedder = embedder
        self.embedding_model = embedder.model_name if embedder else config.EMBEDDING_MODEL

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder(self.embedding_model)
        return self._embedder

    # ------------------------------------------------------------------
    def _check_model(self, conn, record: bool) -> None:
        """資料庫內向量的模型與目前設定不同時拒絕存取：不同模型的向量不可混用。
        record=True（寫入路徑）時，首次寫入會記錄目前的模型。"""
        stored = conn.execute(text("SELECT value FROM index_meta WHERE key = 'embedding_model'")).scalar()
        if stored is None:
            if record:
                conn.execute(
                    text("INSERT INTO index_meta (key, value) VALUES ('embedding_model', :m) ON CONFLICT DO NOTHING"),
                    {"m": self.embedding_model},
                )
        elif stored != self.embedding_model:
            raise IndexStoreError(
                f"資料庫中的向量由 {stored} 產生，與目前設定的 EMBEDDING_MODEL={self.embedding_model} 不同；"
                "請執行 `python -m src.worker reembed` 重算向量。"
            )

    def _sync_kind(self, kind: str, items: Iterable[_Item]) -> Tuple[int, int]:
        """讓資料庫中 `kind` 類項目恰好等於 items：新增缺少的、刪除多餘的。回傳 (新增數, 刪除數)。"""
        desired: Dict[str, _Item] = {}
        for item in items:
            desired[item[0]] = item  # 同 id 後者覆蓋前者（與先前 upsert 行為一致）
        try:
            with self._engine.begin() as conn:
                self._check_model(conn, record=True)
                rows = conn.execute(
                    text("SELECT id, content, acl FROM retrieval_items WHERE kind = :k"), {"k": kind}
                ).all()
                existing = {r.id: (r.content, sorted(r.acl or [])) for r in rows}
                stale = [i for i in existing if i not in desired]
                # 內容不同（例如卡片同 id 但摘要被更新）也要重算向量
                changed = [it for i, it in desired.items() if i not in existing or existing[i][0] != it[3]]
                changed_ids = {it[0] for it in changed}
                # 只有權限變了：不重算向量，只改 acl
                acl_only = [
                    it for i, it in desired.items()
                    if i in existing and i not in changed_ids and existing[i][1] != it[5]
                ]
                if stale:
                    conn.execute(text("DELETE FROM retrieval_items WHERE id = ANY(:ids)"), {"ids": stale})
                if acl_only:
                    conn.execute(
                        text("UPDATE retrieval_items SET acl = :acl WHERE id = :id"),
                        [{"id": it[0], "acl": it[5]} for it in acl_only],
                    )
                for start in range(0, len(changed), WRITE_BATCH):
                    batch = changed[start : start + WRITE_BATCH]
                    vectors = self.embedder([it[3] for it in batch])
                    conn.execute(
                        text(
                            """
                            INSERT INTO retrieval_items (id, kind, doc_type, content, metadata, embedding, acl)
                            VALUES (:id, :kind, :doc_type, :content, CAST(:metadata AS jsonb),
                                    CAST(:embedding AS vector), :acl)
                            ON CONFLICT (id) DO UPDATE SET
                                kind = EXCLUDED.kind, doc_type = EXCLUDED.doc_type, content = EXCLUDED.content,
                                metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding, acl = EXCLUDED.acl
                            """
                        ),
                        [
                            {
                                "id": it[0],
                                "kind": it[1],
                                "doc_type": it[2],
                                "content": it[3],
                                "metadata": json.dumps(it[4], ensure_ascii=False),
                                "embedding": _vector_literal(vec),
                                "acl": it[5],
                            }
                            for it, vec in zip(batch, vectors)
                        ],
                    )
        except IndexStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IndexStoreError(f"寫入檢索索引失敗：{exc}") from exc
        logger.info("索引 %s：新增/更新 %d 筆、刪除 %d 筆，目前 %d 筆。", kind, len(changed), len(stale), len(desired))
        return len(changed), len(stale)

    def sync_cards(
        self, cards: List[FeatureCard], acl_for: Optional[Callable[[FeatureCard], Optional[List[str]]]] = None
    ) -> Tuple[int, int]:
        """讓索引中的功能卡片恰好等於 cards（通常是快取中的全部卡片）。acl_for 決定每張卡片誰能看。"""
        return self._sync_kind(
            "card",
            (
                (_card_id(c), "card", c.doc_type, c.to_embedding_text(), c.to_metadata(),
                 _norm_acl(acl_for(c) if acl_for else None))
                for c in cards
            ),
        )

    def sync_chunks(self, chunks: List[Chunk]) -> Tuple[int, int]:
        """讓索引中的原文段落恰好等於本次抽取的段落（來源移除或內容變動後不殘留舊段落）。"""
        return self._sync_kind(
            "chunk",
            (
                (c.chunk_id, "chunk", c.doc_type, c.to_embedding_text(), c.to_metadata(), _norm_acl(c.acl))
                for c in chunks
            ),
        )

    def query(self, query_text: str, top_k: int = 8, principals: Optional[Sequence[str]] = None) -> List[Dict]:
        """相似度檢索（卡片與原文段落混合），回傳依相似度排序的結果列表。
        principals 有值時只回傳 acl 與它有交集的項目（先過濾再排序）；None＝不過濾（僅供後台工具與測試）。"""
        return self.query_with_denied(query_text, top_k, principals)[0]

    def query_with_denied(
        self, query_text: str, top_k: int = 8, principals: Optional[Sequence[str]] = None
    ) -> Tuple[List[Dict], int]:
        """同 query，另回傳「不看權限時會排進前 top_k、但這位使用者沒權限」的筆數（只記在操作紀錄，不回傳內容）。"""
        if not query_text or not query_text.strip():
            raise IndexStoreError("查詢文字不可為空。")
        vector = _vector_literal(self.embedder([query_text])[0])
        denied = 0
        try:
            with self._engine.begin() as conn:
                self._check_model(conn, record=False)
                where = "" if principals is None else "WHERE acl && CAST(:p AS text[])"
                params = {"q": vector, "k": top_k, "p": list(principals or [])}
                rows = conn.execute(
                    text(
                        f"""
                        SELECT id, content, metadata, embedding <=> CAST(:q AS vector) AS distance
                        FROM retrieval_items
                        {where}
                        ORDER BY distance
                        LIMIT :k
                        """
                    ),
                    params,
                ).all()
                if principals is not None:
                    denied = conn.execute(
                        text(
                            """
                            SELECT count(*) FROM (
                                SELECT acl FROM retrieval_items
                                ORDER BY embedding <=> CAST(:q AS vector)
                                LIMIT :k
                            ) top WHERE NOT (acl && CAST(:p AS text[]))
                            """
                        ),
                        params,
                    ).scalar_one()
        except IndexStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IndexStoreError(f"向量檢索失敗：{exc}") from exc
        hits = [
            {"id": r.id, "document": r.content, "metadata": r.metadata or {}, "distance": r.distance}
            for r in rows
        ]
        return hits, int(denied)

    def count(self, kind: Optional[str] = None) -> int:
        """索引筆數；kind 為 "card" / "chunk" 時只計算該類項目。"""
        try:
            with self._engine.connect() as conn:
                if kind is None:
                    return conn.execute(text("SELECT count(*) FROM retrieval_items")).scalar_one()
                return conn.execute(
                    text("SELECT count(*) FROM retrieval_items WHERE kind = :k"), {"k": kind}
                ).scalar_one()
        except Exception as exc:  # noqa: BLE001
            raise IndexStoreError(f"讀取檢索索引筆數失敗：{exc}") from exc

    def reembed(self) -> int:
        """以目前的 EMBEDDING_MODEL 重算所有向量（更換同維度模型後使用），回傳重算筆數。"""
        try:
            with self._engine.begin() as conn:
                rows = conn.execute(text("SELECT id, content FROM retrieval_items")).all()
                for start in range(0, len(rows), WRITE_BATCH):
                    batch = rows[start : start + WRITE_BATCH]
                    vectors = self.embedder([r.content for r in batch])
                    conn.execute(
                        text("UPDATE retrieval_items SET embedding = CAST(:e AS vector) WHERE id = :id"),
                        [{"id": r.id, "e": _vector_literal(v)} for r, v in zip(batch, vectors)],
                    )
                conn.execute(
                    text(
                        "INSERT INTO index_meta (key, value) VALUES ('embedding_model', :m) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                    ),
                    {"m": self.embedding_model},
                )
        except Exception as exc:  # noqa: BLE001
            raise IndexStoreError(f"重算向量失敗：{exc}") from exc
        return len(rows)
