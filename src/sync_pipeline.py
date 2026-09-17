"""
同步協調器：串接 GitHubExtractor → 功能卡片快取 (CardCache) →
Summarizer (RAP LLM) → VectorStore。

設計目標：Streamlit Community Cloud 的檔案系統是 ephemeral，每次休眠喚醒或
重新部署都會清空本地的 `chroma_data/`。若同步流程每次都對「所有」抓到的
文件重新呼叫 RAP LLM 摘要，會不必要地重複消耗國網 AI RAP 額度。

流程：
    1. 抓取 GitHub 上目前的 README / API 規格 / 近期已合併 PR（免費，PyGithub）。
    2. 讀取寫回 GitHub repo 的功能卡片快取（免費，PyGithub 讀檔；未設定
       `CACHE_REPO` 則略過，等同每次全量重新摘要）。
    3. 快取採「累積」策略：只對「快取中不存在」的文件（新 PR、或內容有變動
       的 README/API 規格）呼叫 RAP LLM 摘要（唯一會消耗額度的步驟）；已存在
       的快取項目永久保留，即使該文件之後不在 GitHub 抓取的時間窗內
       （例如 PR_FETCH_LIMIT 之外的舊 PR），也不會遺失。
    4. 把快取中「全部」卡片（含本次重用與新摘要）重新寫入 ChromaDB——
       embedding 用 ChromaDB 內建免費模型，即使向量資料庫被 ephemeral
       環境清空，也能用快取零成本（不花 RAP 額度）重建。
    5. 若有新增摘要，才把合併後的快取寫回 GitHub，供下次啟動使用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from src import config
from src.card_cache import CardCache, CardCacheError, doc_identity
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
    total_in_db: int
    cache_used: bool
    cache_warning: Optional[str] = None


def run_sync(vector_store: VectorStore, progress: Optional[ProgressFn] = None) -> SyncResult:
    """執行一次完整同步，回傳統計摘要。失敗時拋出對應例外，訊息可直接呈現給使用者。"""

    def _report(msg: str) -> None:
        if progress:
            progress(msg)

    config.require_valid(require_github=True, require_rap=True)

    _report(f"連線 GitHub 並抓取 `{config.GITHUB_REPO}` 的 README / API 規格 / 近期已合併 PR ...")
    extractor = GitHubExtractor()
    docs = extractor.extract_all()
    if not docs:
        raise GitHubExtractorError("未抓取到任何 README / API 規格 / 已合併 PR，請確認倉庫內容或權限設定。")
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
                token=config.GITHUB_TOKEN,
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

    all_cards = [FeatureCard(**fields) for fields in cache.values()]
    if not all_cards:
        raise SummarizerError("目前沒有任何可用的功能卡片，且所有文件摘要皆失敗，請檢查 RAP 連線設定。")

    _report("寫入向量資料庫 (ChromaDB) ...")
    vector_store.upsert_cards(all_cards)
    total_in_db = vector_store.count()
    _report(f"目前資料庫共 {total_in_db} 筆功能卡片（快取累積總數 {len(all_cards)} 筆）。")

    if card_cache is not None and new_count > 0:
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
        total_in_db=total_in_db,
        cache_used=cache_used and card_cache is not None,
        cache_warning=cache_warning,
    )
