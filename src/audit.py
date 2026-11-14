"""
操作紀錄（audit_log）、回饋（feedback）與試點週報。

為什麼要有：
- 訪談第 19 題「主管需不需要看到誰讓 AI 看了什麼」；飛書 aily 的審計日誌可依時間／事件／操作人查詢，
  保存期預設 180 天（見 brain outputs/reports/2026-11-14-feishu-lessons.md）。
- 訪談第 20 題「試用兩週看到什麼結果算值得」：週報的「有用比例」就是試點的成功指標與停損依據。

只記錄問題文字與引用來源的 id，不記錄回答全文，降低紀錄本身變成資料外洩點的風險。
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from sqlalchemy import Engine, text

from src import config

logger = config.get_logger(__name__)


def record_question(
    engine: Engine,
    user_email: str,
    question: str,
    source_ids: Sequence[str],
    denied_count: int,
    error: Optional[str] = None,
    action: str = "answer",
    no_source: Optional[bool] = None,
) -> Optional[int]:
    """寫入一筆操作紀錄，回傳 id。寫入失敗只記 log、回傳 None：紀錄失敗不應擋住員工使用。
    no_source：回答沒有引用任何資料（檢索一定會回傳最接近的幾筆，所以要看回答本身有沒有 [n]）；
    未提供時以 source_ids 是否為空判斷。"""
    try:
        with engine.begin() as conn:
            return conn.execute(
                text(
                    """
                    INSERT INTO audit_log (user_email, action, question, source_ids, denied_count, no_source, error)
                    VALUES (:u, :a, :q, CAST(:s AS jsonb), :d, :n, :e)
                    RETURNING id
                    """
                ),
                {
                    "u": user_email.lower(),
                    "a": action,
                    "q": question,
                    "s": json.dumps(list(source_ids)),
                    "d": int(denied_count),
                    "n": (not source_ids) if no_source is None else bool(no_source),
                    "e": error,
                },
            ).scalar_one()
    except Exception as exc:  # noqa: BLE001
        logger.error("寫入操作紀錄失敗：%s", exc)
        return None


def record_feedback(engine: Engine, audit_id: int, user_email: str, helpful: bool, comment: str = "") -> bool:
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO feedback (audit_id, user_email, helpful, comment) VALUES (:a, :u, :h, :c)"
                ),
                {"a": audit_id, "u": user_email.lower(), "h": helpful, "c": comment or None},
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("寫入回饋失敗：%s", exc)
        return False


def purge_old(engine: Engine, retention_days: int) -> int:
    """刪除超過保存天數的紀錄（回饋隨之刪除），回傳刪除筆數。由背景工作執行（網頁角色沒有刪除權限）。"""
    with engine.begin() as conn:
        return conn.execute(
            text("DELETE FROM audit_log WHERE at < now() - make_interval(days => :d)"), {"d": retention_days}
        ).rowcount


@dataclass
class UsageReport:
    since: dt.datetime
    until: dt.datetime
    questions: int
    active_users: int
    answered_with_sources: int
    questions_with_denied: int
    feedback_count: int
    helpful_count: int
    unhelpful_questions: List[str] = field(default_factory=list)
    no_source_questions: List[str] = field(default_factory=list)

    @property
    def helpful_ratio(self) -> Optional[float]:
        return self.helpful_count / self.feedback_count if self.feedback_count else None

    def to_markdown(self) -> str:
        ratio = f"{self.helpful_ratio:.0%}" if self.helpful_ratio is not None else "?（尚無回饋）"
        lines = [
            f"# 使用週報 {self.since:%Y-%m-%d}～{self.until:%Y-%m-%d}",
            "",
            f"- 提問數：{self.questions}；使用人數：{self.active_users}",
            f"- 有找到資料的提問：{self.answered_with_sources}／{self.questions}",
            f"- 有用比例（試點成功指標）：{ratio}（{self.helpful_count}／{self.feedback_count} 則回饋）",
            f"- 有相關資料但提問者沒權限的提問：{self.questions_with_denied}（只給管理員看，用來檢查權限是否太緊）",
            "",
            "## 被標「沒幫助」的問題（優先改善）",
            *([f"- {q}" for q in self.unhelpful_questions] or ["- （無）"]),
            "",
            "## 找不到資料的問題（可能要補資料來源）",
            *([f"- {q}" for q in self.no_source_questions] or ["- （無）"]),
        ]
        return "\n".join(lines)


def usage_report(engine: Engine, days: int = 7, now: Optional[dt.datetime] = None, limit: int = 10) -> UsageReport:
    until = now or dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(days=days)
    p = {"s": since, "u": until, "l": limit}
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT count(*) AS q,
                       count(DISTINCT user_email) AS users,
                       count(*) FILTER (WHERE NOT no_source AND error IS NULL) AS with_src,
                       count(*) FILTER (WHERE denied_count > 0) AS denied
                FROM audit_log WHERE at >= :s AND at < :u AND action = 'answer'
                """
            ),
            p,
        ).one()
        fb = conn.execute(
            text(
                """
                SELECT count(*) AS n, count(*) FILTER (WHERE f.helpful) AS ok
                FROM feedback f JOIN audit_log a ON a.id = f.audit_id
                WHERE a.at >= :s AND a.at < :u
                """
            ),
            p,
        ).one()
        unhelpful = conn.execute(
            text(
                """
                SELECT a.question FROM feedback f JOIN audit_log a ON a.id = f.audit_id
                WHERE a.at >= :s AND a.at < :u AND NOT f.helpful ORDER BY a.at DESC LIMIT :l
                """
            ),
            p,
        ).scalars().all()
        no_source = conn.execute(
            text(
                """
                SELECT question FROM audit_log
                WHERE at >= :s AND at < :u AND no_source AND error IS NULL ORDER BY at DESC LIMIT :l
                """
            ),
            p,
        ).scalars().all()
    return UsageReport(
        since=since,
        until=until,
        questions=row.q,
        active_users=row.users,
        answered_with_sources=row.with_src,
        questions_with_denied=row.denied,
        feedback_count=fb.n,
        helpful_count=fb.ok,
        unhelpful_questions=list(unhelpful),
        no_source_questions=list(no_source),
    )
