"""權限、操作紀錄、回饋：
- retrieval_items.acl：誰能看這筆（主體字串陣列，見 src/tenant.py），檢索時先過濾再排序。
- audit_log：每次提問記錄誰、問什麼、引用哪些來源、被權限擋掉幾筆。
- feedback：員工對回答按「有用／沒幫助」，週報算有用比例（試點成功指標）。

網頁服務的角色 pmec_app 原本只能讀；這兩張表要讓它寫入（只限 INSERT，不能改、不能刪）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 既有資料預設所有登入者可看，與加入權限前的行為相同；下次同步會依 tenant.toml 改寫
    op.execute("ALTER TABLE retrieval_items ADD COLUMN acl text[] NOT NULL DEFAULT '{*}'")
    op.execute("CREATE INDEX retrieval_items_acl_idx ON retrieval_items USING gin (acl)")
    op.execute(
        """
        CREATE TABLE audit_log (
            id            bigserial PRIMARY KEY,
            at            timestamptz NOT NULL DEFAULT now(),
            user_email    text NOT NULL,
            action        text NOT NULL DEFAULT 'answer',
            question      text NOT NULL,
            source_ids    jsonb NOT NULL DEFAULT '[]',
            denied_count  integer NOT NULL DEFAULT 0,
            no_source     boolean NOT NULL DEFAULT false,
            error         text
        )
        """
    )
    op.execute("CREATE INDEX audit_log_at_idx ON audit_log (at)")
    op.execute(
        """
        CREATE TABLE feedback (
            id          bigserial PRIMARY KEY,
            audit_id    bigint NOT NULL REFERENCES audit_log (id) ON DELETE CASCADE,
            at          timestamptz NOT NULL DEFAULT now(),
            user_email  text NOT NULL,
            helpful     boolean NOT NULL,
            comment     text
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pmec_app') THEN
                GRANT SELECT, INSERT ON audit_log, feedback TO pmec_app;
                GRANT USAGE ON SEQUENCE audit_log_id_seq, feedback_id_seq TO pmec_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE feedback, audit_log")
    op.execute("DROP INDEX retrieval_items_acl_idx")
    op.execute("ALTER TABLE retrieval_items DROP COLUMN acl")
