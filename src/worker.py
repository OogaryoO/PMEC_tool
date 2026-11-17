"""
背景工作入口（Cloud Run Job / 本機命令列）：

    python -m src.worker migrate   # 升級資料庫 schema；有設定 PMEC_APP_DB_PASSWORD 時一併建立/更新網頁用的唯讀角色
    python -m src.worker sync      # 同步知識庫（抓取 → 摘要 → 更新索引），結果記錄在 sync_runs
    python -m src.worker reembed   # 更換 EMBEDDING_MODEL 後重算所有向量
    python -m src.worker report [--days 7]   # 印出使用週報（提問數、有用比例、找不到資料的問題）
    python -m src.worker purge-audit         # 刪除超過 tenant.toml audit.retention_days 的操作紀錄
    python -m src.worker check-tenant        # 檢查 tenant.toml，印出連接器、權限規則與階段
    python -m src.worker scheduler           # 常駐：處理網頁的同步請求＋每日同步（不在 Google Cloud 上時用）

失敗時以非零狀態碼結束，讓 Cloud Run Job 標記為失敗。
"""
from __future__ import annotations

import argparse
import os
import sys

from src import config
from src.db import ensure_app_role, get_engine, upgrade_schema
from src.index_store import IndexStore

logger = config.get_logger("src.worker")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.worker")
    parser.add_argument("command", choices=["migrate", "sync", "reembed", "report", "purge-audit", "check-tenant", "scheduler"])
    parser.add_argument("--trigger", default="", help="sync 記錄在 sync_runs 的觸發來源")
    parser.add_argument("--days", type=int, default=7, help="report 的統計天數")
    args = parser.parse_args(argv)

    if args.command == "check-tenant":
        from src.connectors import build_connectors
        from src.tenant import STAGE_NAMES, load_tenant, tenant_path

        tenant = load_tenant()
        print(f"設定檔：{tenant_path()}（{'存在' if tenant_path().exists() else '不存在，使用預設值'}）")
        print(f"企業：{tenant.name}；階段 {tenant.stage}（{STAGE_NAMES[tenant.stage]}）")
        print("連接器：" + "、".join(c.describe() for c in build_connectors(tenant.connectors)))
        print(f"預設權限：{tenant.default_acl}；沿用來源權限：{tenant.inherit_source_permissions}")
        for rule in tenant.acl_rules:
            print(f"  規則 {rule.match} → {rule.allow}")
        print(f"排除：{tenant.exclude or '（無）'}")
        print(f"群組：{ {g: len(m) for g, m in tenant.groups.items()} }")
        print(f"操作紀錄保存：{tenant.audit_retention_days} 天")
        return 0

    if args.command == "migrate":
        upgrade_schema()
        logger.info("資料庫 schema 已升級到最新版本。")
        app_password = os.getenv("PMEC_APP_DB_PASSWORD", "")
        if app_password:
            ensure_app_role(get_engine(), app_password)
            logger.info("網頁服務資料庫角色 pmec_app 已就緒。")
        return 0

    engine = get_engine()
    index_store = IndexStore(engine)
    if args.command == "sync":
        from src.sync_pipeline import run_recorded_sync

        # Cloud Run Job 執行時帶有 CLOUD_RUN_EXECUTION；由排程或網頁觸發皆屬此類
        from src.sync_pipeline import SyncAlreadyRunning

        trigger = args.trigger or "job:" + os.getenv("CLOUD_RUN_EXECUTION", "manual")
        try:
            result = run_recorded_sync(engine, index_store, trigger=trigger)
        except SyncAlreadyRunning as exc:
            logger.info("%s", exc)
            return 0
        except Exception:  # noqa: BLE001 - 錯誤已記錄在 sync_runs，這裡只決定結束碼
            logger.exception("同步失敗。")
            return 1
        logger.info("同步完成：%s", result)
        return 0

    if args.command == "scheduler":
        from src.scheduler import run_forever

        run_forever(engine, index_store)
        return 0

    if args.command == "report":
        from src.audit import usage_report

        print(usage_report(engine, days=args.days).to_markdown())
        return 0

    if args.command == "purge-audit":
        from src.audit import purge_old
        from src.tenant import get_tenant

        days = get_tenant().audit_retention_days
        logger.info("已刪除 %d 筆超過 %d 天的操作紀錄。", purge_old(engine, days), days)
        return 0

    count = index_store.reembed()
    logger.info("已用 %s 重算 %d 筆向量。", index_store.embedding_model, count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
