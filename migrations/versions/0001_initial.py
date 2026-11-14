"""初始 schema：功能卡片快取、檢索項目（卡片＋原文段落，pgvector）、同步紀錄。

Revision ID: 0001
Revises:
Create Date: 2026-10-03
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# 向量維度需與 config.EMBEDDING_MODEL 一致（paraphrase-multilingual-MiniLM-L12-v2 = 384）；
# 換成不同維度的模型需要新的 migration。
EMBEDDING_DIM = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE feature_cards (
            cache_key  text PRIMARY KEY,
            card       jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE retrieval_items (
            id        text PRIMARY KEY,
            kind      text NOT NULL CHECK (kind IN ('card', 'chunk')),
            doc_type  text NOT NULL,
            content   text NOT NULL,
            metadata  jsonb NOT NULL,
            embedding vector({EMBEDDING_DIM}) NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX retrieval_items_kind_idx ON retrieval_items (kind)")
    op.execute("CREATE TABLE index_meta (key text PRIMARY KEY, value text NOT NULL)")
    op.execute(
        """
        CREATE TABLE sync_runs (
            id          bigserial PRIMARY KEY,
            trigger     text NOT NULL,
            status      text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            started_at  timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            summary     jsonb,
            error       text
        )
        """
    )
    # 網頁服務的資料庫角色（deploy/gcp_setup.sh 建立）只需讀取；本機開發沒有這個角色則略過。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pmec_app') THEN
                GRANT SELECT ON feature_cards, retrieval_items, index_meta, sync_runs TO pmec_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE sync_runs, index_meta, retrieval_items, feature_cards")
