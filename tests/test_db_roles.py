"""網頁用的資料庫角色 pmec_app：只能讀，外加新增操作紀錄、回饋、同步請求；不能改、不能刪知識庫。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from src.db import ensure_app_role


@pytest.fixture
def app_engine(engine, db_url):
    ensure_app_role(engine, "app-test-pw")
    eng = create_engine(make_url(db_url).set(username="pmec_app", password="app-test-pw"))
    yield eng
    eng.dispose()


def test_app_role_can_read_and_insert_allowed_tables(app_engine):
    with app_engine.begin() as c:
        c.execute(text("SELECT count(*) FROM retrieval_items")).scalar_one()
        c.execute(text("INSERT INTO sync_requests (requested_by) VALUES ('t@example.com')"))
        c.execute(text("INSERT INTO audit_log (user_email, question) VALUES ('t@example.com', 'q')"))


@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE sync_requests SET picked_at = now()",
        "DELETE FROM retrieval_items",
        "DELETE FROM audit_log",
        "INSERT INTO sync_runs (trigger, status) VALUES ('x', 'running')",
        "TRUNCATE feature_cards",
    ],
)
def test_app_role_cannot_modify(app_engine, stmt):
    with pytest.raises(Exception, match="permission denied|InsufficientPrivilege|must be owner"):
        with app_engine.begin() as c:
            c.execute(text(stmt))
