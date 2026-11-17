"""G2：網頁同步請求佇列＋背景排程＋同一時間只跑一個同步。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from src import scheduler, sync_pipeline

TZ = ZoneInfo("Asia/Taipei")


@dataclass
class _Result:
    doc_count: int = 0


@pytest.fixture
def fake_sync(monkeypatch):
    calls = []

    def _run_sync(engine, index_store, progress=None):
        calls.append(1)
        return _Result()

    monkeypatch.setattr(sync_pipeline, "run_sync", _run_sync)
    monkeypatch.setattr("src.audit.purge_old", lambda engine, days: 0)
    return calls


def _runs(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT trigger, status FROM sync_runs ORDER BY id")).all()


def test_web_request_is_picked_up_once(engine, fake_sync, monkeypatch):
    monkeypatch.setenv("SYNC_DAILY_AT", "")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE sync_requests"))
    sync_pipeline.request_sync(engine, "kai@example.com")
    sync_pipeline.request_sync(engine, "kai@example.com")
    assert sync_pipeline.pending_sync_requests(engine) == 2
    assert scheduler.tick(engine, index_store=None) == "web:kai@example.com"
    assert sync_pipeline.pending_sync_requests(engine) == 0
    assert scheduler.tick(engine, index_store=None) is None
    assert fake_sync == [1]
    assert _runs(engine) == [("web:kai@example.com", "succeeded")]


def test_daily_sync_runs_once_per_day(engine, fake_sync, monkeypatch):
    monkeypatch.setenv("SYNC_DAILY_AT", "03:00")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE sync_requests"))
    early = dt.datetime(2026, 11, 18, 2, 59, tzinfo=TZ)
    assert scheduler.tick(engine, None, now=early) is None
    # sync_runs.started_at 用資料庫 now()，所以「今天」以真實時間判斷：用今天的 03:01 測
    today_after = dt.datetime.now(TZ).replace(hour=23, minute=59)
    assert scheduler.tick(engine, None, now=today_after) == scheduler.DAILY_TRIGGER
    assert scheduler.tick(engine, None, now=today_after) is None
    assert len(fake_sync) == 1


def test_only_one_sync_at_a_time(engine, fake_sync):
    holder = engine.connect()
    try:
        assert holder.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": sync_pipeline.SYNC_LOCK_KEY}).scalar_one()
        with pytest.raises(sync_pipeline.SyncAlreadyRunning):
            sync_pipeline.run_recorded_sync(engine, None, trigger="manual")
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": sync_pipeline.SYNC_LOCK_KEY})
        holder.close()
    sync_pipeline.run_recorded_sync(engine, None, trigger="manual")  # 鎖放開後可以跑
    assert fake_sync == [1]
