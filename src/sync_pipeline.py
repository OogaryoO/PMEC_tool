"""
同步協調器：串接連接器（src/connectors.py：GitHub、Google Drive、共用資料夾…）→ 功能卡片快取 (feature_cards) →
Summarizer (RAP LLM) → IndexStore (pgvector)。每家企業用哪些連接器、權限與排除規則見 tenant.toml（src/tenant.py）。

流程：
    1. 抓取 GitHub 上目前的 README / API 規格 / 近期已合併 PR；
       若設定 GDRIVE_FOLDER_IDS，另抓取 Google Drive 資料夾（含子資料夾）文件。
    2. 讀取資料庫中的功能卡片快取。
    3. 快取採「累積」策略：只對「快取中不存在」的文件（新 PR、或內容有變動
       的 README/API 規格）呼叫 RAP LLM 摘要（唯一會消耗額度的步驟）；已存在
       的快取項目永久保留，即使該文件之後不在 GitHub 抓取的時間窗內
       （例如 PR_FETCH_LIMIT 之外的舊 PR），也不會遺失。
    4. 新摘要先寫回快取，再更新檢索索引（索引寫入失敗也不會浪費已花的 RAP 額度）。
    5. 索引中的卡片與原文段落以差異更新：只為新內容計算 embedding，移除已不存在的項目。

Drive 卡片例外：同檔案只保留最新版本，檔案移出資料夾（掃描完整時）即自快取與索引移除。

每次執行都記錄在 sync_runs 表，供網頁顯示上次同步結果。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import Engine, text

from src import config
from src.card_cache import doc_identity, drop_file_entries, load_cards, prune_file_entries, save_cards
from src.chunker import chunk_document, source_ref_of
from src.connectors import Connector, ConnectorError, ConnectorResult, build_connectors
from src.documents import ExtractedDocument, source_path_of
from src.index_store import IndexStore
from src.summarizer import FeatureCard, Summarizer, SummarizerError
from src.tenant import TenantConfig, get_tenant

logger = config.get_logger(__name__)

ProgressFn = Callable[[str], None]


@dataclass
class SyncResult:
    doc_count: int
    reused_count: int
    new_count: int
    failed_count: int
    removed_count: int
    card_count: int
    chunk_count: int
    embedded_count: int
    excluded_count: int = 0


def _extract_sources(
    connectors: List[Connector], tenant: TenantConfig, report: ProgressFn
) -> Tuple[List[ExtractedDocument], List[ConnectorResult], Dict[str, Set[str]]]:
    """逐一執行連接器；套用排除規則。回傳 (文件, 各連接器結果, {doc_type: 被排除的 file_id})。"""
    docs: List[ExtractedDocument] = []
    results: List[ConnectorResult] = []
    excluded_ids: Dict[str, Set[str]] = {}
    excluded_count = 0
    for connector in connectors:
        report(f"連線 {connector.describe()} 並抓取文件 ...")
        result = connector.extract()
        kept = []
        for doc in result.docs:
            if tenant.is_excluded(doc.source_path):
                excluded_count += 1
                if "file_id" in doc.metadata:
                    excluded_ids.setdefault(doc.doc_type, set()).add(doc.metadata["file_id"])
                continue
            kept.append(doc)
        result.docs = kept
        results.append(result)
        docs.extend(kept)
        report(f"{connector.describe()}：讀取 {len(kept)} 份，略過 {len(result.skipped)} 份（不支援的格式或無法讀取）。")
        for path in result.truncated:
            report(f"⚠️ 「{path}」內容過長，只收錄前段（超出部分無法被檢索）。")
        if not result.complete:
            report(f"⚠️ {connector.describe()} 掃描未完整（達上限或部分資料夾讀取失敗），本次不會移除已刪除檔案的舊卡片。")
    if excluded_count:
        report(f"依 tenant.toml 的排除規則，丟棄 {excluded_count} 份文件（不摘要、不索引）。")
    if not docs:
        raise ConnectorError("所有資料來源都沒有抓到文件，請確認來源內容、權限設定與排除規則。")
    # PostgreSQL 的 text / jsonb 不接受 NUL 字元（部分 PDF、二進位誤判的檔案會帶有）
    for doc in docs:
        doc.title = doc.title.replace("\x00", "")
        doc.content = doc.content.replace("\x00", "")
        doc.files = [f.replace("\x00", "") for f in doc.files]
    return docs, results, excluded_ids


def run_sync(
    engine: Engine,
    index_store: IndexStore,
    progress: Optional[ProgressFn] = None,
    tenant: Optional[TenantConfig] = None,
    connectors: Optional[List[Connector]] = None,
    summarizer: Optional[Summarizer] = None,
) -> SyncResult:
    """執行一次完整同步，回傳統計摘要。失敗時拋出對應例外，訊息可直接呈現給使用者。
    tenant／connectors／summarizer 預設由設定檔建立；測試時可注入。"""

    def _report(msg: str) -> None:
        logger.info(msg)
        if progress:
            progress(msg)

    tenant = tenant or get_tenant()
    if connectors is None:
        connectors = build_connectors(tenant.connectors)
    # engine 已由呼叫端建立，這裡不再檢查 DATABASE_URL
    config.require_valid(require_github=False, require_rap=summarizer is None)

    docs, results, excluded_ids = _extract_sources(connectors, tenant, _report)
    _report(f"共抓取到 {len(docs)} 份原始文件。")

    cache = load_cards(engine)
    _report(f"讀取功能卡片快取：已有 {len(cache)} 筆先前摘要結果。")

    docs_to_summarize = [doc for doc in docs if doc_identity(doc) not in cache]
    reused_count = len(docs) - len(docs_to_summarize)
    _report(
        f"其中 {reused_count} 份已有快取可重複使用（不呼叫 RAP），"
        f"{len(docs_to_summarize)} 份需要呼叫 RAP LLM 重新摘要。"
    )

    new_count = 0
    failed_count = 0
    if docs_to_summarize:
        summarizer = summarizer or Summarizer()

        def _progress(done: int, total: int, doc) -> None:
            _report(f"摘要進度 {done}/{total}：{doc.title[:40]}")

        results_ = summarizer.summarize_batch(
            docs_to_summarize, repo_name=config.GITHUB_REPO, progress_callback=_progress
        )
        for doc, card in results_:
            if card is None:
                failed_count += 1
                continue
            cache[doc_identity(doc)] = card.to_dict()
            new_count += 1
    _report(f"成功新增 {new_count} 張功能卡片（{failed_count} 份文件摘要失敗已略過）。")

    removed_count = 0
    for doc_type, ids in excluded_ids.items():
        removed_count += len(drop_file_entries(cache, doc_type, ids))
    for result in results:
        if result.doc_type:
            removed_count += len(
                prune_file_entries(cache, result.docs, result.seen_file_ids, result.complete, doc_type=result.doc_type)
            )
    if removed_count:
        _report(f"移除 {removed_count} 張過期（文件已更新、已刪除或被排除）的卡片。")

    if not cache:
        raise SummarizerError("目前沒有任何可用的功能卡片，且所有文件摘要皆失敗，請檢查 RAP 連線設定。")
    save_cards(engine, cache)

    # 權限：文件依 tenant 規則（或來源權限）決定 acl；卡片沿用同一份文件的 acl，
    # 文件已不在本次抓取範圍（例如舊 PR）時依規則比對卡片的路徑
    acl_by_ref = {}
    chunks = []
    for doc in docs:
        acl = tenant.resolve_acl(doc.source_path, doc.source_acl)
        acl_by_ref[(doc.doc_type, source_ref_of(doc))] = acl
        for chunk in chunk_document(doc):
            chunk.acl = acl
            chunks.append(chunk)

    def _card_acl(card: FeatureCard) -> List[str]:
        found = acl_by_ref.get((card.doc_type, card.source_ref))
        if found is not None:
            return found
        path = source_path_of(card.doc_type, card.files, card.source_title)
        return tenant.resolve_acl(path)

    all_cards = [FeatureCard(**fields) for fields in cache.values()]
    # 卡片也要套排除規則（排除規則晚於快取建立時，舊卡片不能留在索引）
    all_cards = [
        c for c in all_cards if not tenant.is_excluded(source_path_of(c.doc_type, c.files, c.source_title))
    ]
    _report("更新檢索索引（本地 embedding，不消耗 RAP）...")
    card_embedded, _ = index_store.sync_cards(all_cards, acl_for=_card_acl)
    chunk_embedded, _ = index_store.sync_chunks(chunks)
    chunk_count = index_store.count("chunk")
    card_count = index_store.count("card")
    _report(
        f"索引共 {card_count + chunk_count} 筆（功能卡片 {card_count} 張、原文段落 {chunk_count} 段），"
        f"本次重算向量 {card_embedded + chunk_embedded} 筆。"
    )

    return SyncResult(
        doc_count=len(docs),
        reused_count=reused_count,
        new_count=new_count,
        failed_count=failed_count,
        removed_count=removed_count,
        card_count=card_count,
        chunk_count=chunk_count,
        embedded_count=card_embedded + chunk_embedded,
        excluded_count=sum(len(v) for v in excluded_ids.values()),
    )


# pg_advisory_lock 的鍵：同一個資料庫同一時間只跑一個同步（排程、網頁請求、手動指令都適用）
SYNC_LOCK_KEY = 72_7274_01


class SyncAlreadyRunning(Exception):
    """已有其他同步在進行中。"""


def run_recorded_sync(
    engine: Engine, index_store: IndexStore, trigger: str, progress: Optional[ProgressFn] = None
) -> SyncResult:
    """執行同步並把開始、結果或錯誤記錄到 sync_runs；例外照常往外拋。
    同一個資料庫同時只允許一個同步，已有同步在跑時拋出 SyncAlreadyRunning。"""
    lock_conn = engine.connect()
    try:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SYNC_LOCK_KEY}).scalar_one():
            raise SyncAlreadyRunning("已有同步正在進行，請等它完成。")
        lock_conn.commit()
        return _run_recorded_sync_locked(engine, index_store, trigger, progress)
    finally:
        try:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SYNC_LOCK_KEY})
            lock_conn.commit()
        finally:
            lock_conn.close()


def _run_recorded_sync_locked(
    engine: Engine, index_store: IndexStore, trigger: str, progress: Optional[ProgressFn] = None
) -> SyncResult:
    with engine.begin() as conn:
        run_id = conn.execute(
            text("INSERT INTO sync_runs (trigger, status) VALUES (:t, 'running') RETURNING id"), {"t": trigger}
        ).scalar_one()
    try:
        result = run_sync(engine, index_store, progress)
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE sync_runs SET status = 'failed', finished_at = now(), error = :e WHERE id = :id"),
                {"e": f"{type(exc).__name__}: {exc}", "id": run_id},
            )
        raise
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sync_runs SET status = 'succeeded', finished_at = now(), "
                "summary = CAST(:s AS jsonb) WHERE id = :id"
            ),
            {"s": json.dumps(asdict(result)), "id": run_id},
        )
    return result


def request_sync(engine: Engine, requested_by: str) -> None:
    """網頁按「同步」：只新增一筆請求，由背景排程執行（網頁角色沒有寫入知識庫的權限）。"""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sync_requests (requested_by) VALUES (:u)"), {"u": requested_by})


def pending_sync_requests(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM sync_requests WHERE picked_at IS NULL")).scalar_one()


def take_sync_requests(engine: Engine) -> list:
    """取出所有待處理請求（標記 picked_at），回傳請求者清單；沒有則回傳空清單。"""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "UPDATE sync_requests SET picked_at = now() WHERE picked_at IS NULL "
                "RETURNING requested_by"
            )
        ).scalars().all()
    return list(rows)


def last_sync_run(engine: Engine) -> Optional[dict]:
    """最近一次同步紀錄（含進行中），沒有紀錄則回傳 None。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT trigger, status, started_at, finished_at, summary, error "
                "FROM sync_runs ORDER BY id DESC LIMIT 1"
            )
        ).mappings().first()
    return dict(row) if row else None
