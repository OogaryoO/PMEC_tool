"""
本機開發用：在 .dev_db/ 啟動內嵌 PostgreSQL（pgserver，內含 pgvector），升級 schema，
並印出可填入 .env 的 DATABASE_URL。資料庫在背景持續執行，重複執行此腳本會沿用同一份資料。

    pip install -r requirements-dev.txt
    python scripts/dev_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pgserver  # noqa: E402

from src.db import upgrade_schema  # noqa: E402


def main() -> None:
    server = pgserver.get_server(ROOT / ".dev_db", cleanup_mode=None)
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    upgrade_schema(url)
    print(f"DATABASE_URL={url}")


if __name__ == "__main__":
    main()
