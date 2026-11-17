"""
背景排程（不在 Google Cloud 上時用；Cloud Run 版用 Cloud Scheduler＋Cloud Run Job）：

    python -m src.worker scheduler

每 SYNC_POLL_SECONDS 秒（預設 30）檢查一次：
  1. 網頁「同步」按鈕留下的請求（sync_requests）→ 馬上同步
  2. 每天 SYNC_DAILY_AT（預設 03:00，時區 SYNC_TIMEZONE，預設 Asia/Taipei）→ 同步一次，並刪除超過保存天數的操作紀錄
     SYNC_DAILY_AT 設為空字串＝不排每日同步
同一時間只會有一個同步（src/sync_pipeline.py 的 advisory lock），排程重啟後不會重複跑當天的每日同步。
"""
from __future__ import annotations

import datetime as dt
import os
import time
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from src import config
from src.index_store import IndexStore

logger = config.get_logger("src.scheduler")

DAILY_TRIGGER = "schedule:daily"


def _daily_time() -> Optional[dt.time]:
    raw = os.getenv("SYNC_DAILY_AT", "03:00").strip()
    if not raw:
        return None
    hh, mm = raw.split(":")
    return dt.time(int(hh), int(mm))


def _tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("SYNC_TIMEZONE", "Asia/Taipei"))


def daily_already_ran(engine: Engine, today: dt.date, tz: ZoneInfo) -> bool:
    with engine.connect() as conn:
        last = conn.execute(
            text("SELECT max(started_at) FROM sync_runs WHERE trigger = :t"), {"t": DAILY_TRIGGER}
        ).scalar_one()
    return last is not None and last.astimezone(tz).date() >= today


def tick(engine: Engine, index_store: IndexStore, now: Optional[dt.datetime] = None) -> Optional[str]:
    """執行一輪檢查；有做事時回傳觸發來源（測試用），沒事回傳 None。"""
    from src.audit import purge_old
    from src.sync_pipeline import SyncAlreadyRunning, run_recorded_sync, take_sync_requests
    from src.tenant import get_tenant

    tz = _tz()
    now = (now or dt.datetime.now(tz)).astimezone(tz)
    daily_at = _daily_time()
    due_daily = daily_at is not None and now.time() >= daily_at and not daily_already_ran(engine, now.date(), tz)
    requesters = take_sync_requests(engine)
    if not requesters and not due_daily:
        return None

    trigger = DAILY_TRIGGER if due_daily else "web:" + ",".join(sorted(set(requesters)))
    try:
        result = run_recorded_sync(engine, index_store, trigger=trigger)
        logger.info("同步完成（%s）：%s", trigger, result)
    except SyncAlreadyRunning:
        logger.info("已有同步在進行，這輪略過（%s）。", trigger)
    except Exception:  # noqa: BLE001 - 錯誤已記錄在 sync_runs；排程繼續跑
        logger.exception("同步失敗（%s）。", trigger)
    if due_daily:
        days = get_tenant().audit_retention_days
        logger.info("已刪除 %d 筆超過 %d 天的操作紀錄。", purge_old(engine, days), days)
    return trigger


def run_forever(engine: Engine, index_store: IndexStore) -> None:
    poll = int(os.getenv("SYNC_POLL_SECONDS", "30"))
    logger.info("排程啟動：每 %d 秒檢查同步請求；每日同步 %s（%s）。",
                poll, os.getenv("SYNC_DAILY_AT", "03:00") or "關閉", _tz().key)
    while True:
        try:
            tick(engine, index_store)
        except Exception:  # noqa: BLE001 - 資料庫暫時斷線等，下輪再試
            logger.exception("排程檢查失敗，%d 秒後重試。", poll)
        time.sleep(poll)
