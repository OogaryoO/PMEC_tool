"""同步請求佇列：網頁的「同步」按鈕只新增一筆請求，由背景排程（src/scheduler.py）取出執行。

網頁服務的角色 pmec_app 沒有寫入知識庫的權限，所以不能自己跑同步；
它只能在這張表「新增」請求（不能改、不能刪），寫入仍只發生在背景工作。

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sync_requests (
            id            bigserial PRIMARY KEY,
            requested_at  timestamptz NOT NULL DEFAULT now(),
            requested_by  text NOT NULL,
            picked_at     timestamptz
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pmec_app') THEN
                GRANT SELECT, INSERT ON sync_requests TO pmec_app;
                GRANT USAGE ON SEQUENCE sync_requests_id_seq TO pmec_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE sync_requests")
