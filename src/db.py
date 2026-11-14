"""
PostgreSQL（含 pgvector）連線與 schema 遷移。

- 連線字串來自 config.DATABASE_URL（SQLAlchemy URL，driver 用 psycopg 3）。
- schema 以 Alembic 管理（migrations/），`python -m src.worker migrate` 升級到最新版本；
  網頁服務不做遷移，避免多個執行個體同時改 schema。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Engine, create_engine, text

from src import config

MIGRATIONS_DIR = config.PROJECT_ROOT / "migrations"


_engine: Optional[Engine] = None


def _normalize_url(url: str) -> str:
    # 允許填 postgresql:// 或 postgres://，統一改用 psycopg 3 driver
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def get_engine(url: Optional[str] = None) -> Engine:
    """未指定 url 時回傳以 config.DATABASE_URL 建立、全程序共用的 engine。"""
    global _engine
    if url is not None:
        return create_engine(_normalize_url(url), pool_pre_ping=True)
    if _engine is None:
        config.require_valid(require_github=False, require_rap=False, require_database=True)
        _engine = create_engine(_normalize_url(config.DATABASE_URL), pool_pre_ping=True, pool_size=5)
    return _engine


def upgrade_schema(url: Optional[str] = None) -> None:
    """把資料庫 schema 升級到最新版本（Alembic head）。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Alembic 的 ini 插值會處理 %，連線字串內的 % 需跳脫
    cfg.set_main_option("sqlalchemy.url", _normalize_url(url or config.DATABASE_URL).replace("%", "%%"))
    command.upgrade(cfg, "head")


APP_ROLE = "pmec_app"


def ensure_app_role(engine: Engine, password: str) -> None:
    """建立（或更新密碼）網頁服務使用的資料庫角色：只能登入與讀取，非 superuser、不可繞過 RLS。
    以 schema 擁有者（worker 角色）執行。"""
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}).scalar()
        verb = "ALTER" if exists else "CREATE"
        quoted = conn.execute(text("SELECT quote_literal(:p)"), {"p": password}).scalar_one()
        conn.execute(text(f"{verb} ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {quoted}"))
        db = conn.execute(text("SELECT current_database()")).scalar_one()
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {APP_ROLE}'))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"))
        # 例外：操作紀錄與回饋由網頁服務寫入（只能新增，不能修改或刪除）
        has_audit = conn.execute(text("SELECT to_regclass('public.audit_log') IS NOT NULL")).scalar_one()
        if has_audit:
            conn.execute(text(f"GRANT INSERT ON audit_log, feedback TO {APP_ROLE}"))
            conn.execute(text(f"GRANT USAGE ON SEQUENCE audit_log_id_seq, feedback_id_seq TO {APP_ROLE}"))
