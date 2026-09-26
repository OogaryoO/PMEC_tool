"""
呼叫 RAP LLM（相容 OpenAI SDK）將原始的 README / API 規格 / PR / Google Drive 文件
轉譯為 PM 看得懂的「功能卡片」：

    - 功能名稱 (feature_name)
    - 商業場景支援 (business_scenario)
    - 目前狀態 (status)：已上線 / 開發中 / 限制

輸出會被規格化成 FeatureCard，供 vector_store.py 入庫。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import openai
from openai import OpenAI

from src import config
from src.chunker import source_ref_of
from src.documents import ExtractedDocument

logger = config.get_logger(__name__)


class SummarizerError(Exception):
    """呼叫 RAP LLM 或解析回應失敗時拋出，訊息需可直接呈現給使用者。"""


SYSTEM_PROMPT = (
    "你是一位資深的技術 PM 助理，任務是把工程師的原始程式碼變更、PR 說明、"
    "README、API 規格或組織內部文件，「翻譯」成業務人員 (PM / Sales / 客戶) 看得懂的功能卡片。"
    "你只輸出 JSON，不要輸出任何多餘的文字、Markdown 標記或程式碼區塊符號。"
)

USER_PROMPT_TEMPLATE = """\
請閱讀以下來自 GitHub 倉庫「{repo_name}」的原始資料，並整理成一張「功能卡片」。

來源資料：
---
{doc_text}
---

請嚴格以下列 JSON 格式輸出（欄位皆為繁體中文內容，key 保持英文）：
{{
  "feature_name": "功能名稱，簡短、商業導向，避免技術術語",
  "business_scenario": "這個功能可以支援哪些商業場景／客戶需求，用 1-3 句話說明",
  "status": "已上線 / 開發中 / 限制，三選一，並可附註簡短原因",
  "key_points": ["條列 1-3 個重點，例如限制條件、涵蓋範圍"]
}}

注意事項：
1. 若來源資料看起來只是雜務性變更（例如格式調整、CI 設定、依賴升級），
   請將 feature_name 標註為「(非功能性變更)」，business_scenario 可簡短說明原因，status 設為 "已上線"。
2. 禁止杜撰不存在的功能或誇大狀態；資料不足時請在 status 或 key_points 中誠實反映。
3. 只輸出 JSON，不要有任何前後綴文字。
"""

DRIVE_USER_PROMPT_TEMPLATE = """\
請閱讀以下來自組織 Google Drive 的內部文件，整理成一張「功能卡片」，重點是這份文件揭露了我們能對客戶提供的產品能力、服務範圍、流程或限制。

來源資料：
---
{doc_text}
---

請嚴格以下列 JSON 格式輸出（欄位皆為繁體中文內容，key 保持英文）：
{{
  "feature_name": "文件描述的主要功能／服務／主題名稱，簡短、商業導向，避免技術術語",
  "business_scenario": "這份文件可以支援哪些商業場景／客戶需求的判斷，用 1-3 句話說明",
  "status": "已上線 / 開發中 / 限制，三選一，依文件內容判斷並附註簡短原因；若文件只是規劃或提案，填「開發中」並註明（規劃文件）",
  "key_points": ["條列 1-3 個重點，例如承諾條件、關鍵數字（價格、SLA、上限）、適用範圍"]
}}

注意事項：
1. 若文件與產品能力或對客戶承諾無關（例如行政公告、會議室預約、個人筆記），
   請將 feature_name 標註為「(非產品相關文件)」，business_scenario 簡短說明文件性質，status 設為 "不適用"。
2. 禁止杜撰文件中不存在的內容或誇大狀態；資料不足時請在 status 或 key_points 中誠實反映。
3. 只輸出 JSON，不要有任何前後綴文字。
"""

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class FeatureCard:
    """規格化後的功能卡片，準備寫入向量資料庫。"""

    feature_name: str
    business_scenario: str
    status: str
    key_points: List[str] = field(default_factory=list)

    doc_type: str = ""
    source_title: str = ""
    source_ref: str = ""  # PR URL 或檔案路徑
    files: List[str] = field(default_factory=list)

    def to_embedding_text(self) -> str:
        """組合成適合做語意檢索的文字（存入 ChromaDB documents）。"""
        points = "；".join(self.key_points) if self.key_points else ""
        return (
            f"功能名稱：{self.feature_name}\n"
            f"商業場景支援：{self.business_scenario}\n"
            f"目前狀態：{self.status}\n"
            f"重點：{points}\n"
            f"資料來源：{self.source_title}"
        )

    def to_metadata(self) -> Dict[str, str]:
        """ChromaDB metadata 只接受純量型別，這裡統一轉成字串。"""
        return {
            "feature_name": self.feature_name,
            "business_scenario": self.business_scenario,
            "status": self.status,
            "key_points": " | ".join(self.key_points),
            "doc_type": self.doc_type,
            "source_title": self.source_title,
            "source_ref": self.source_ref,
            "files": ", ".join(self.files[:20]),
        }

    def to_dict(self) -> Dict:
        return asdict(self)


class Summarizer:
    """封裝與 RAP LLM 的互動，將 ExtractedDocument 轉為 FeatureCard。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or config.RAP_API_KEY
        self.base_url = base_url or config.RAP_BASE_URL
        self.model_name = model_name or config.RAP_MODEL_NAME

        if not self.api_key:
            raise SummarizerError("RAP_API_KEY 未設定，請在 .env 中設定 RAP 平台的 API Key。")
        if not self.base_url:
            raise SummarizerError("RAP_BASE_URL 未設定，請確認 RAP 端點網址。")

        try:
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=config.LLM_REQUEST_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            raise SummarizerError(f"初始化 RAP LLM 客戶端失敗：{exc}") from exc

    # ------------------------------------------------------------------
    def _call_llm(self, user_prompt: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=config.LLM_TEMPERATURE,
            )
        except openai.AuthenticationError as exc:
            raise SummarizerError(
                "RAP API Key 驗證失敗，請確認 RAP_API_KEY 是否正確、是否已過期。"
            ) from exc
        except openai.APIConnectionError as exc:
            raise SummarizerError(
                f"無法連線至 RAP 端點 ({self.base_url})，請確認網路狀況與端點網址是否正確。"
            ) from exc
        except openai.RateLimitError as exc:
            raise SummarizerError("RAP LLM 已達速率限制，請稍後再試。") from exc
        except openai.APIStatusError as exc:
            raise SummarizerError(f"RAP LLM 回應錯誤 (status={exc.status_code})：{exc.message}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SummarizerError(f"呼叫 RAP LLM 時發生未預期錯誤：{exc}") from exc

        if not response.choices:
            raise SummarizerError("RAP LLM 回應為空，請稍後再試。")

        content = response.choices[0].message.content
        if not content:
            raise SummarizerError("RAP LLM 回應內容為空，請稍後再試。")
        return content

    @staticmethod
    def _parse_json_response(raw_text: str) -> Dict:
        text = raw_text.strip()
        # 去除可能的 ```json ... ``` 包裹
        text = re.sub(r"^```(json)?", "", text.strip(), flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text.strip()).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = _JSON_BLOCK_RE.search(text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise SummarizerError(f"無法解析 RAP LLM 回傳的 JSON：{exc}\n原始回應：{raw_text[:500]}") from exc

        raise SummarizerError(f"RAP LLM 回應非預期格式（找不到 JSON）：{raw_text[:500]}")

    def summarize_document(self, doc: ExtractedDocument, repo_name: str) -> FeatureCard:
        """將單一 ExtractedDocument 轉為 FeatureCard。"""
        if doc.doc_type == "gdrive":
            prompt = DRIVE_USER_PROMPT_TEMPLATE.format(doc_text=doc.to_llm_text())
        else:
            prompt = USER_PROMPT_TEMPLATE.format(repo_name=repo_name, doc_text=doc.to_llm_text())
        raw = self._call_llm(prompt)
        parsed = self._parse_json_response(raw)

        feature_name = str(parsed.get("feature_name") or "未命名功能").strip()
        business_scenario = str(parsed.get("business_scenario") or "（無說明）").strip()
        status = str(parsed.get("status") or "未知").strip()
        key_points_raw = parsed.get("key_points") or []
        if isinstance(key_points_raw, str):
            key_points = [key_points_raw]
        else:
            key_points = [str(p) for p in key_points_raw]

        source_ref = source_ref_of(doc)

        return FeatureCard(
            feature_name=feature_name,
            business_scenario=business_scenario,
            status=status,
            key_points=key_points,
            doc_type=doc.doc_type,
            source_title=doc.title,
            source_ref=source_ref,
            files=doc.files,
        )

    def summarize_batch(
        self,
        docs: List[ExtractedDocument],
        repo_name: str,
        progress_callback: Optional[Callable[[int, int, ExtractedDocument], None]] = None,
    ) -> List[Tuple[ExtractedDocument, Optional[FeatureCard]]]:
        """批次轉譯，單一文件失敗不中斷整批，改為記錄警告並跳過。

        回傳與輸入 `docs` 順序一致的 (原始文件, 卡片) 配對；摘要失敗的項目
        卡片為 None，讓呼叫端可以依原始文件反查（例如寫入摘要快取）而不需
        另外猜測文件與卡片的對應關係。
        """
        results: List[Tuple[ExtractedDocument, Optional[FeatureCard]]] = []
        total = len(docs)

        for idx, doc in enumerate(docs, start=1):
            try:
                card = self.summarize_document(doc, repo_name=repo_name)
                results.append((doc, card))
            except SummarizerError as exc:
                logger.warning("摘要文件「%s」失敗，已略過：%s", doc.title, exc)
                results.append((doc, None))
            finally:
                if progress_callback:
                    progress_callback(idx, total, doc)

        if docs and not any(card for _, card in results):
            raise SummarizerError("所有文件摘要皆失敗，請檢查 RAP 連線設定與模型名稱是否正確。")

        return results
