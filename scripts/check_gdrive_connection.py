"""
本地手動驗證腳本：確認 Google Drive 設定（Service Account 金鑰、資料夾共用權限）
真的能讀到 GDRIVE_FOLDER_IDS 指定資料夾（含子資料夾）內的文件。

執行方式（在專案根目錄，venv 已啟用）：
    python scripts/check_gdrive_connection.py

會依序：
    1. 檢查 GDRIVE_FOLDER_IDS / GDRIVE_SERVICE_ACCOUNT_JSON 設定是否齊全。
    2. 以 Service Account 金鑰建立 Drive 客戶端，印出 Service Account email
       （資料夾必須共用給這個 email）。
    3. 掃描所有資料夾，逐份列出讀取到的文件與略過的檔案。

不呼叫 RAP LLM、不讀寫功能卡片快取、不動 ChromaDB。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.gdrive_extractor import GDriveExtractor, GDriveExtractorError


def _fail(step: str, exc: BaseException) -> None:
    print(f"\n[FAIL] {step}")
    print(f"       {type(exc).__name__}: {exc}")
    traceback.print_exc()
    sys.exit(1)


def main() -> None:
    print(f"GDRIVE_FOLDER_IDS: {config.GDRIVE_FOLDER_IDS}")
    print(f"GDRIVE_MAX_FILES: {config.GDRIVE_MAX_FILES}")
    missing = config.validate(require_github=False, require_rap=False, require_gdrive=True)
    if missing:
        print("\n[FAIL] Google Drive 設定不完整：")
        for m in missing:
            print(f"  - {m}")
        sys.exit(1)

    try:
        extractor = GDriveExtractor()
    except GDriveExtractorError as exc:
        _fail("建立 Google Drive 客戶端", exc)
    print(f"Service Account: {extractor.service_account_email}")
    print("（資料夾必須共用給上面這個 email，檢視者即可）\n")

    try:
        result = extractor.extract_all()
    except GDriveExtractorError as exc:
        _fail("掃描 Google Drive 資料夾", exc)

    print(f"讀取到的文件（{len(result.docs)} 份）：")
    for doc in result.docs:
        print(f"- {doc.title}  ({len(doc.content)} 字, {doc.metadata['mime_type']})")

    print(f"\n略過的檔案（{len(result.skipped)} 份）：")
    for item in result.skipped:
        print(f"- {item}")

    if result.truncated:
        print(f"\n內容過長、只收錄前段的檔案（{len(result.truncated)} 份）：")
        for path in result.truncated:
            print(f"- {path}")

    print(f"\n掃描完整: {result.complete}")
    print(f"[OK] Google Drive 讀取正常：{len(result.docs)} 份文件")


if __name__ == "__main__":
    main()
