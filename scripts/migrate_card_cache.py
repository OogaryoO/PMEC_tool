"""
一次性搬移：把舊版寫在 GitHub repo 的功能卡片快取（JSON 檔）匯入資料庫 feature_cards，
避免搬到新架構後所有文件都得重新呼叫 RAP 摘要。

執行方式（專案根目錄，DATABASE_URL 指向目標資料庫，schema 已 migrate）：
    python scripts/migrate_card_cache.py --repo OogaryoO/pmec-cache --token <有讀取權限的 token>
    # 或改用本機已下載的檔案：
    python scripts/migrate_card_cache.py --file feature_cards.json

只新增資料庫中還沒有的快取鍵，可重複執行。匯入後執行 `python -m src.worker sync` 建立檢索索引。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.card_cache import load_cards, save_cards
from src.db import get_engine
from src.summarizer import FeatureCard


def _load_from_github(repo: str, path: str, token: str, branch: str) -> str:
    from github import Auth, Github

    kwargs = {"ref": branch} if branch else {}
    content = Github(auth=Auth.Token(token), timeout=30).get_repo(repo).get_contents(path, **kwargs)
    return content.decoded_content.decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="舊快取所在的 repo（owner/repo）")
    src.add_argument("--file", help="本機的快取 JSON 檔")
    parser.add_argument("--path", default="pmec_cache/feature_cards.json")
    parser.add_argument("--branch", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    if args.repo:
        if not args.token:
            parser.error("--repo 需搭配 --token")
        raw = _load_from_github(args.repo, args.path, args.token, args.branch)
    else:
        raw = Path(args.file).read_text(encoding="utf-8")

    legacy = json.loads(raw)
    if not isinstance(legacy, dict):
        sys.exit("快取檔格式非預期（不是 JSON object）。")
    for key, fields in legacy.items():
        FeatureCard(**fields)  # 欄位不符時直接報錯，不寫入半套資料

    engine = get_engine()
    cache = load_cards(engine)
    before = len(cache)
    for key, fields in legacy.items():
        cache.setdefault(key, fields)
    save_cards(engine, cache)
    print(f"舊快取 {len(legacy)} 筆；資料庫原有 {before} 筆，新增 {len(cache) - before} 筆，現有 {len(cache)} 筆。")


if __name__ == "__main__":
    main()
