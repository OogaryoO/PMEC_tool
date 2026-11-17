"""
本地手動驗證腳本：確認目前程式碼中「呼叫 AI 模型」相關的三條路徑
都真的能打通，而不是只看程式碼邏輯猜測。

執行方式（在專案根目錄，venv 已啟用）：
    python scripts/check_llm_connection.py

會依序做三層驗證，任何一層失敗都會印出完整錯誤訊息並中止：
    1. 連線測試：用 .env 的 LLM_PROVIDER／LLM_MODEL／LLM_API_KEY（或舊的 RAP_*）
       打一次模型，確認組合正確、模型名稱存在。
    2. Summarizer 路徑：餵一份假造的 ExtractedDocument，跑過
       summarizer.py 的 prompt 組裝 -> 呼叫 LLM -> JSON 解析，確認
       「功能卡片摘要」這條路徑產出的物件欄位正確。
    3. RAGEngine 路徑：用只含這張假卡片的記憶體內檢索來源，跑過 rag_engine.py 的
       組 context -> 呼叫 LLM，確認「PM 問答」這條路徑能產生回答。

不需要連 GitHub，也不會讀寫資料庫。會消耗少量模型額度（三次呼叫）。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.documents import ExtractedDocument
from src.rag_engine import RAGEngine, RAGEngineError
from src.summarizer import Summarizer, SummarizerError


def _fail(step: str, exc: BaseException) -> None:
    print(f"\n[FAIL] {step}")
    print(f"       {type(exc).__name__}: {exc}")
    traceback.print_exc()
    sys.exit(1)


def step1_raw_connection() -> None:
    from src.llm import LLMClient, LLMError, load_settings, missing_settings

    s = load_settings()
    print("=== 1/3 連線測試（客戶選的 AI 模型） ===")
    print(f"    LLM_PROVIDER = {s.provider or '（未設定）'}")
    print(f"    LLM_MODEL    = {s.model or '（未設定）'}")
    print(f"    LLM_BASE_URL = {s.base_url or '（官方 SDK 預設）'}")
    print(f"    LLM_API_KEY  = {'已設定' if s.api_key else '未設定'}（len={len(s.api_key)}）")
    problems = missing_settings(s)
    if problems:
        _fail("step1", RuntimeError("；".join(problems)))
    try:
        content = LLMClient(s).chat("你是測試助理。", [{"role": "user", "content": "請只回覆兩個字：測試成功"}])
    except LLMError as exc:
        _fail("step1 chat", exc)
    print(f"    模型回覆: {content!r}")
    print("[OK] 連線測試通過\n")


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
    class _SingleCardIndex:
        """只回傳這張卡片的檢索來源，讓驗證不依賴資料庫。"""

        def query(self, query_text: str, top_k: int = 8):
            metadata = {**card.to_metadata(), "kind": "card"}
            return [{"id": "check", "document": card.to_embedding_text(), "metadata": metadata, "distance": 0.0}]

        def query_with_denied(self, query_text: str, top_k: int = 8, principals=None):
            return self.query(query_text, top_k), 0

    try:
        engine = RAGEngine(index_store=_SingleCardIndex(), repo_name="example/example")
        answer = engine.answer("目前系統支援 CSV 批量匯入名單嗎？")
    except RAGEngineError as exc:
        _fail("step3 RAGEngine", exc)
    except Exception as exc:  # noqa: BLE001
        _fail("step3 RAGEngine (未預期例外)", exc)

    print(f"    檢索到 {len(answer.retrieved)} 張卡片")
    print("    --- markdown ---")
    print("    " + answer.markdown.replace("\n", "\n    "))
    assert answer.markdown.strip(), "模型應回傳非空回答"
    print("[OK] RAGEngine 路徑通過\n")


def main() -> None:
    step1_raw_connection()
    card = step2_summarizer()
    step3_rag_engine(card)
    print("全部三層驗證皆通過：AI 模型呼叫相關程式碼在目前 .env 設定下可正常運作。")


if __name__ == "__main__":
    main()
