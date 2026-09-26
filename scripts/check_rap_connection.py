"""
本地手動驗證腳本：確認目前程式碼中「呼叫 RAP LLM」相關的三條路徑
都真的能打通，而不是只看程式碼邏輯猜測。

執行方式（在專案根目錄，venv 已啟用）：
    python scripts/check_rap_connection.py

會依序做三層驗證，任何一層失敗都會印出完整錯誤訊息並中止：
    1. 裸連線測試：直接用 openai.OpenAI 打 RAP_BASE_URL，確認
       RAP_API_KEY / RAP_BASE_URL / RAP_MODEL_NAME 三者組合是正確、
       有效、且該模型名稱在 RAP 平台上真的存在。
    2. Summarizer 路徑：餵一份假造的 ExtractedDocument，跑過
       summarizer.py 的 prompt 組裝 -> 呼叫 LLM -> JSON 解析，確認
       「功能卡片摘要」這條路徑產出的物件欄位正確。
    3. RAGEngine 路徑：用一個暫存目錄建立 VectorStore，塞入一張假卡片，
       跑過 rag_engine.py 的檢索 -> 組 context -> 呼叫 LLM -> 區塊切段，
       確認「PM 問答」這條路徑的四個區塊都能被正確解析出來。

不需要真的連 GitHub，也不會動到 .env 指定的正式 CHROMA_PERSIST_DIR
（VectorStore 會被導向一個臨時目錄）。會消耗少量 RAP 額度（三次 LLM 呼叫）。
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI

from src import config
from src.documents import ExtractedDocument
from src.rag_engine import RAGEngine, RAGEngineError
from src.summarizer import Summarizer, SummarizerError
from src.vector_store import VectorStore, VectorStoreError


def _fail(step: str, exc: BaseException) -> None:
    print(f"\n[FAIL] {step}")
    print(f"       {type(exc).__name__}: {exc}")
    traceback.print_exc()
    sys.exit(1)


def step1_raw_connection() -> None:
    print("=== 1/3 裸連線測試 (openai client -> RAP_BASE_URL) ===")
    print(f"    RAP_BASE_URL   = {config.RAP_BASE_URL}")
    print(f"    RAP_MODEL_NAME = {config.RAP_MODEL_NAME}")
    print(f"    RAP_API_KEY    = {config.RAP_API_KEY[:6]}...(len={len(config.RAP_API_KEY)})")

    if not config.RAP_API_KEY or not config.RAP_BASE_URL:
        _fail("step1", RuntimeError("RAP_API_KEY / RAP_BASE_URL 未設定，請檢查 .env"))

    client = OpenAI(api_key=config.RAP_API_KEY, base_url=config.RAP_BASE_URL, timeout=config.LLM_REQUEST_TIMEOUT)
    try:
        resp = client.chat.completions.create(
            model=config.RAP_MODEL_NAME,
            messages=[{"role": "user", "content": "請只回覆兩個字：測試成功"}],
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        _fail("step1 chat.completions.create", exc)

    content = resp.choices[0].message.content if resp.choices else None
    if not content:
        _fail("step1", RuntimeError("回應為空 choices/content"))
    print(f"    模型回覆: {content.strip()!r}")
    print("[OK] 裸連線測試通過\n")


def step2_summarizer() -> None:
    print("=== 2/3 Summarizer 路徑 (summarize_document) ===")
    doc = ExtractedDocument(
        doc_type="pr",
        title="feat: 新增 CSV 批量匯入名單 API",
        content=(
            "新增 POST /api/contacts/import 端點，接受 multipart/form-data 上傳的 CSV 檔，"
            "逐列驗證欄位後批次寫入資料庫，回傳成功筆數與失敗列的錯誤原因。"
            "目前上限為單檔 5000 列，超過會回傳 400。"
        ),
        files=["src/routes/contacts_import.py"],
        metadata={"url": "https://github.com/example/example/pull/123"},
    )

    try:
        summarizer = Summarizer()
        card = summarizer.summarize_document(doc, repo_name="example/example")
    except SummarizerError as exc:
        _fail("step2 Summarizer", exc)
    except Exception as exc:  # noqa: BLE001
        _fail("step2 Summarizer (未預期例外)", exc)

    print(f"    feature_name       = {card.feature_name!r}")
    print(f"    business_scenario  = {card.business_scenario!r}")
    print(f"    status             = {card.status!r}")
    print(f"    key_points         = {card.key_points!r}")
    assert card.feature_name and card.feature_name != "未命名功能", "feature_name 應被 LLM 填出具體內容"
    print("[OK] Summarizer 路徑通過\n")
    return card


def step3_rag_engine(card) -> None:
    print("=== 3/3 RAGEngine 路徑 (answer) ===")
    with tempfile.TemporaryDirectory(prefix="pmec_rap_check_") as tmpdir:
        try:
            vs = VectorStore(persist_dir=tmpdir, collection_name="rap_check")
            vs.upsert_cards([card])
        except VectorStoreError as exc:
            _fail("step3 VectorStore", exc)

        try:
            engine = RAGEngine(vector_store=vs, repo_name="example/example")
            answer = engine.answer("目前系統支援 CSV 批量匯入名單嗎？")
        except RAGEngineError as exc:
            _fail("step3 RAGEngine", exc)
        except Exception as exc:  # noqa: BLE001
            _fail("step3 RAGEngine (未預期例外)", exc)

    print(f"    檢索到 {len(answer.retrieved)} 張卡片")
    print("    --- markdown ---")
    print("    " + answer.markdown.replace("\n", "\n    "))
    assert answer.markdown.strip(), "RAP LLM 應回傳非空回答"
    print("[OK] RAGEngine 路徑通過\n")


def main() -> None:
    step1_raw_connection()
    card = step2_summarizer()
    step3_rag_engine(card)
    print("全部三層驗證皆通過：RAP LLM 呼叫相關程式碼在目前 .env 設定下可正常運作。")


if __name__ == "__main__":
    main()
