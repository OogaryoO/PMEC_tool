"""
同步協調器：串接 GitHubExtractor / GDriveExtractor → 功能卡片快取 (CardCache) →
Summarizer (RAP LLM) → VectorStore。

設計目標：Streamlit Community Cloud 的檔案系統是 ephemeral，每次休眠喚醒或
重新部署都會清空本地的 `chroma_data/`。若同步流程每次都對「所有」抓到的
文件重新呼叫 RAP LLM 摘要，會不必要地重複消耗國網 AI RAP 額度。

流程：
    1. 抓取 GitHub 上目前的 README / API 規格 / 近期已合併 PR（免費，PyGithub）；
       若設定 GDRIVE_FOLDER_IDS，另抓取 Google Drive 資料夾（含子資料夾）文件。
    2. 讀取寫回 GitHub repo 的功能卡片快取（免費，PyGithub 讀檔；未設定
       `CACHE_REPO` 則略過，等同每次全量重新摘要）。
    3. 快取採「累積」策略：只對「快取中不存在」的文件（新 PR、或內容有變動
       的 README/API 規格）呼叫 RAP LLM 摘要（唯一會消耗額度的步驟）；已存在
       的快取項目永久保留，即使該文件之後不在 GitHub 抓取的時間窗內
       （例如 PR_FETCH_LIMIT 之外的舊 PR），也不會遺失。
    4. 把快取中「全部」卡片（含本次重用與新摘要）重新寫入 ChromaDB——
       embedding 用本地免費 ONNX 模型，即使向量資料庫被 ephemeral
       環境清空，也能用快取零成本（不花 RAP 額度）重建。
    5. 把本次抓到的所有文件原文切段 (chunker) 寫入 ChromaDB，取代舊段落；
       段落不經 RAP，每次都從來源重新產生，不寫入快取。
    6. 若有新增摘要，才把合併後的快取寫回 GitHub，供下次啟動使用。

Drive 卡片例外：同檔案只保留最新版本，檔案移出資料夾（掃描完整時）即自快取與
ChromaDB 移除。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from src import config
from src.card_cache import CardCache, CardCacheError, doc_identity, prune_gdrive_entries
from src.chunker import chunk_document
from src.config import ConfigError
from src.documents import ExtractedDocument
from src.gdrive_extractor import GDriveExtractor, GDriveExtractorError, GDriveExtractResult
from src.github_extractor import GitHubExtractor, GitHubExtractorError
from src.summarizer import FeatureCard, Summarizer, SummarizerError
from src.vector_store import VectorStore

logger = config.get_logger(__name__)

ProgressFn = Callable[[str], None]


@dataclass
class SyncResult:
    doc_count: int
    reused_count: int
    new_count: int
    failed_count: int
    removed_count: int
    chunk_count: int
    total_in_db: int
    cache_used: bool
    cache_warning: Optional[str] = None


def _extract_sources(report: ProgressFn) -> Tuple[List[ExtractedDocument], Optional[GDriveExtractResult]]:
    """抓取 GitHub 與（有設定時）Google Drive 文件；全部為空時拋出對應例外。"""
    report(f"連線 GitHub 並抓取 `{config.GITHUB_REPO}` 的 README / API 規格 / 近期已合併 PR ...")
    docs = GitHubExtractor().extract_all()
    report(f"GitHub：共抓取到 {len(docs)} 份原始文件。")

    drive_result: Optional[GDriveExtractResult] = None
    if config.GDRIVE_ENABLED:
        report(f"連線 Google Drive 並掃描 {len(config.GDRIVE_FOLDER_IDS)} 個資料夾（含子資料夾）...")
        drive_result = GDriveExtractor().extract_all()
        docs.extend(drive_result.docs)
        report(
            f"Google Drive：共讀取 {len(drive_result.docs)} 份文件，"
            f"略過 {len(drive_result.skipped)} 份（不支援的格式或無法讀取）。"
        )
        for path in drive_result.truncated:
            report(f"⚠️ 「{path}」內容過長，只收錄前段（超出部分無法被檢索）。")
        if not drive_result.complete:
            report(
                f"⚠️ Google Drive 掃描未完整（已達 GDRIVE_MAX_FILES={config.GDRIVE_MAX_FILES} 上限或部分子資料夾讀取失敗），"
                "本次不會移除已從 Drive 刪除的舊卡片。"
            )
    if not docs:
        if drive_result is None:
            raise GitHubExtractorError("未抓取到任何 README / API 規格 / 已合併 PR，請確認倉庫內容或權限設定。")
        raise GDriveExtractorError("GitHub 與 Google Drive 皆未抓取到任何文件，請確認倉庫內容、資料夾內容與權限設定。")
    return docs, drive_result


def _index_chunks(vector_store: VectorStore, docs: List[ExtractedDocument], report: ProgressFn) -> int:
    chunks = [chunk for doc in docs for chunk in chunk_document(doc)]
    report(f"將 {len(docs)} 份文件原文切成 {len(chunks)} 段並寫入向量資料庫（本地 embedding，不消耗 RAP）...")
    return vector_store.replace_chunks(chunks)


def run_sync(vector_store: VectorStore, progress: Optional[ProgressFn] = None) -> SyncResult:
    """執行一次完整同步，回傳統計摘要。失敗時拋出對應例外，訊息可直接呈現給使用者。"""

    def _report(msg: str) -> None:
        if progress:
            progress(msg)

    config.require_valid(require_github=True, require_rap=True, require_gdrive=config.GDRIVE_ENABLED)

    docs, drive_result = _extract_sources(_report)
    _report(f"共抓取到 {len(docs)} 份原始文件。")

    cache: dict = {}
    cache_warning: Optional[str] = None
    card_cache: Optional[CardCache] = None
    cache_used = bool(config.CACHE_ENABLED and config.CACHE_REPO)

    if cache_used:
        try:
            card_cache = CardCache(
                repo_full_name=config.CACHE_REPO,
                file_path=config.CACHE_FILE_PATH,
                token=config.CACHE_GITHUB_TOKEN,
                branch=config.CACHE_BRANCH,
            )
            cache = card_cache.load()
            _report(f"讀取功能卡片快取（{config.CACHE_REPO}）：已有 {len(cache)} 筆先前摘要結果。")
        except CardCacheError as exc:
            cache_warning = f"讀取快取失敗，本次將重新摘要所有文件：{exc}"
            logger.warning(cache_warning)
            card_cache = None
            cache = {}
    else:
        _report("未設定 CACHE_REPO，跳過快取（每次同步都會對所有文件重新呼叫 RAP LLM 摘要）。")

    docs_to_summarize = [doc for doc in docs if doc_identity(doc) not in cache]
    reused_count = len(docs) - len(docs_to_summarize)
    _report(
        f"其中 {reused_count} 份已有快取可重複使用（不呼叫 RAP），"
        f"{len(docs_to_summarize)} 份需要呼叫 RAP LLM 重新摘要。"
    )

    new_count = 0
    failed_count = 0
    if docs_to_summarize:
        summarizer = Summarizer()

        def _progress(done: int, total: int, doc) -> None:
            _report(f"摘要進度 {done}/{total}：{doc.title[:40]}")

        results = summarizer.summarize_batch(
            docs_to_summarize, repo_name=config.GITHUB_REPO, progress_callback=_progress
        )
        for doc, card in results:
            if card is None:
                failed_count += 1
                continue
            cache[doc_identity(doc)] = card.to_dict()
            new_count += 1

    _report(f"成功新增 {new_count} 張功能卡片（{failed_count} 份文件摘要失敗已略過）。")

    removed_count = 0
    if drive_result is not None:
        removed_keys = prune_gdrive_entries(
            cache, drive_result.docs, drive_result.seen_file_ids, drive_result.complete
        )
        removed_count = len(removed_keys)
        if removed_count:
            _report(f"移除 {removed_count} 張過期（文件已更新）或已從 Drive 移除的卡片。")

    all_cards = [FeatureCard(**fields) for fields in cache.values()]
    if not all_cards:
        raise SummarizerError("目前沒有任何可用的功能卡片，且所有文件摘要皆失敗，請檢查 RAP 連線設定。")

    _report("寫入向量資料庫 (ChromaDB) ...")
    if drive_result is not None:
        # 先清掉 ChromaDB 中所有 Drive 卡片再以快取重建，讓已移除／過期的卡片不殘留
        vector_store.delete_by_doc_type("gdrive")
    vector_store.upsert_cards(all_cards)
    chunk_count = _index_chunks(vector_store, docs, _report)
    total_in_db = vector_store.count()
    _report(f"目前資料庫共 {total_in_db} 筆（功能卡片 {len(all_cards)} 張、原文段落 {chunk_count} 段）。")

    if card_cache is not None and (new_count > 0 or removed_count > 0):
        try:
            card_cache.save(cache)
            _report("已將更新後的功能卡片快取寫回 GitHub。")
        except CardCacheError as exc:
            warn = f"寫入快取失敗（不影響本次同步結果，但下次啟動可能需要重新摘要這批文件）：{exc}"
            logger.warning(warn)
            cache_warning = warn

    return SyncResult(
        doc_count=len(docs),
        reused_count=reused_count,
        new_count=new_count,
        failed_count=failed_count,
        removed_count=removed_count,
        chunk_count=chunk_count,
        total_in_db=total_in_db,
        cache_used=cache_used and card_cache is not None,
        cache_warning=cache_warning,
    )


@dataclass
class BootstrapResult:
    card_count: int
    chunk_count: int
    total_in_db: int
    chunk_warning: Optional[str] = None


def bootstrap_from_cache(vector_store: VectorStore) -> Optional[BootstrapResult]:
    """用 GitHub 上既有的功能卡片快取還原本地 ChromaDB，並重新抓取來源文件建立原文段落；
    全程不呼叫 RAP LLM。用於 App 冷啟動時本地向量資料庫是空的（ephemeral 檔案系統重建、
    首次啟動或更換 embedding 模型），讓使用者一進入頁面就能直接提問。

    快取未啟用、未設定 CACHE_REPO、或快取內容目前是空的，回傳 None（呼叫端應提示
    使用者改用「同步並更新」建立知識庫）。讀取快取失敗時拋出 CardCacheError。
    抓取來源失敗不影響卡片還原，只回報於 chunk_warning。
    """
    if not (config.CACHE_ENABLED and config.CACHE_REPO):
        return None

    card_cache = CardCache(
        repo_full_name=config.CACHE_REPO,
        file_path=config.CACHE_FILE_PATH,
        token=config.CACHE_GITHUB_TOKEN,
        branch=config.CACHE_BRANCH,
    )
    cache = card_cache.load()
    if not cache:
        return None

    all_cards = [FeatureCard(**fields) for fields in cache.values()]
    vector_store.upsert_cards(all_cards)

    chunk_count = 0
    chunk_warning: Optional[str] = None
    try:
        config.require_valid(require_github=True, require_rap=False, require_gdrive=config.GDRIVE_ENABLED)
        docs, _ = _extract_sources(logger.info)
        chunk_count = _index_chunks(vector_store, docs, logger.info)
    except (ConfigError, GitHubExtractorError, GDriveExtractorError) as exc:
        chunk_warning = f"已從快取還原功能卡片，但抓取原文失敗，目前只能檢索卡片摘要：{exc}"
        logger.warning(chunk_warning)

    return BootstrapResult(
        card_count=len(all_cards),
        chunk_count=chunk_count,
        total_in_db=vector_store.count(),
        chunk_warning=chunk_warning,
    )
