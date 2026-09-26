"""
RAG 引擎：接收使用者提問（可含先前對話）-> 向量檢索相關的功能卡片與原文段落 -> 呼叫 RAP LLM
依問題意圖產出回答。

回答不套用固定範本：由 LLM 先判斷使用者要的是事實查詢、進度狀態、操作說明、
彙整比較、對客戶承諾評估或閒聊，再決定回答形式；引用資料以 [n] 標註，
編號對應 RAGAnswer.retrieved 的順序。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import openai
from openai import OpenAI

from src import config
from src.vector_store import VectorStore, VectorStoreError

logger = config.get_logger(__name__)


class RAGEngineError(Exception):
    """檢索或生成回答失敗時拋出，訊息需可直接呈現給使用者。"""


# 帶入 LLM 的先前問答輪數上限；只帶問答本身、不帶當輪檢索資料，控制 prompt 長度。
MAX_HISTORY_TURNS = 3

SYSTEM_PROMPT_TEMPLATE = """\
你是組織內部的知識庫助理。知識庫內容來自 GitHub 倉庫 {repo_name} 的實作紀錄（README、API 規格、已合併 PR）\
與 Google Drive 內部文件。檢索資料有兩種：「功能卡片」是整份文件的概要；「原文段落」是文件原文的片段\
（試算表以「欄名：值」逐列呈現）。具體細節、數字與時程以原文段落為準。

回答前先判斷使用者真正想知道什麼，再依問題類型決定回答形式：
- 事實查詢（例如「有沒有 X 功能」「X 是什麼」）：第一句直接回答，再說明依據。
- 進度、規劃、狀態：說明目前狀態（已上線／開發中／規劃中）與依據。
- 操作方式、運作原理、比較、彙整：用條列或短段落整理重點。
- 詢問能否對客戶承諾或如何對外說明：有相關紀錄時，評估「可以／有條件可以／目前無法」，說明差距與風險，並提供一段不過度承諾的對外說法；\
沒有相關紀錄時，結論是「無法確認，需向工程團隊確認」，對外說法只表達「將確認後回覆」，不得寫「尚未支援」「不支援」。
- 打招呼、閒聊、詢問你能做什麼：簡短自然回應並說明你能回答的範圍，不必引用資料。
- 延續先前對話的追問（例如「那 P3 呢？」）：結合先前對話理解指涉對象後再回答。
- 語意不明：先依最合理的解讀回答，最後用一句話向使用者確認；完全無法解讀時請使用者補充。

規則：
1. 只能依據本輪提供的「檢索資料」與先前對話回答，禁止杜撰資料中沒有的功能、數字、時程或承諾。
2. 檢索資料與問題無關或不足時，明確說明「知識庫中找不到相關紀錄」，可建議改問方向或需向哪個團隊確認；不要硬套不相關的資料。\
查無紀錄不等於不支援：不可據此斷言「不支援」「尚未支援」，對外說法只能表達「需進一步確認」。
3. 使用資料時在句末以編號標註來源，例如 [1]、[2]，編號對應檢索資料的序號。
4. Google Drive 文件與 GitHub 實作紀錄（README／API 規格／PR）內容衝突時，以 GitHub 實作紀錄為準並指出衝突。
5. 以繁體中文、Markdown 回答；篇幅與問題複雜度相稱，簡單問題簡短回答，不要套用固定標題範本，不要使用程式碼區塊包住整段回答。
"""

USER_PROMPT_TEMPLATE = """\
檢索資料（依相關度排序，可能包含與問題無關的結果）：
---
{context}
---

使用者問題：{question}
"""

SOURCE_TYPE_LABELS = {
    "readme": "GitHub README",
    "api_spec": "GitHub API 規格",
    "pr": "GitHub PR",
    "gdrive": "Google Drive 文件",
}


@dataclass
class RetrievedItem:
    kind: str  # "card"：功能卡片；"chunk"：原文段落
    title: str  # 卡片的功能名稱；段落為空
    summary: str  # 卡片的商業場景說明；段落為原文
    status: str
    key_points: str
    section: str  # 段落所在的區段（工作表名稱、Markdown 標題）
    source_title: str
    source_ref: str
    doc_type: str
    distance: Optional[float]


@dataclass
class RAGAnswer:
    question: str
    markdown: str
    retrieved: List[RetrievedItem] = field(default_factory=list)


class RAGEngine:
    """組合 VectorStore 檢索與 RAP LLM 生成，依使用者意圖產出知識庫回答。"""

    def __init__(
        self,
        vector_store: VectorStore,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        repo_name: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> None:
        self.vector_store = vector_store
        self.repo_name = repo_name or config.GITHUB_REPO or "未指定倉庫"
        self.top_k = top_k or config.RAG_TOP_K

        api_key = api_key or config.RAP_API_KEY
        base_url = base_url or config.RAP_BASE_URL
        self.model_name = model_name or config.RAP_MODEL_NAME

        if not api_key:
            raise RAGEngineError("RAP_API_KEY 未設定，請在 .env 中設定 RAP 平台的 API Key。")
        if not base_url:
            raise RAGEngineError("RAP_BASE_URL 未設定，請確認 RAP 端點網址。")

        try:
            self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=config.LLM_REQUEST_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            raise RAGEngineError(f"初始化 RAP LLM 客戶端失敗：{exc}") from exc

        self._system_prompt = SYSTEM_PROMPT_TEMPLATE.format(repo_name=self.repo_name)

    # ------------------------------------------------------------------
    def _retrieve(self, query: str) -> List[RetrievedItem]:
        try:
            hits = self.vector_store.query(query, top_k=self.top_k)
        except VectorStoreError as exc:
            raise RAGEngineError(str(exc)) from exc

        items: List[RetrievedItem] = []
        for hit in hits:
            meta = hit.get("metadata", {}) or {}
            is_chunk = meta.get("kind") == "chunk"
            items.append(
                RetrievedItem(
                    kind="chunk" if is_chunk else "card",
                    title="" if is_chunk else meta.get("feature_name", "(未知主題)"),
                    # 段落的 document 第一行是 embedding 用的「來源｜區段」標頭，原文從第二行起
                    summary=(hit.get("document") or "").partition("\n")[2]
                    if is_chunk
                    else meta.get("business_scenario", ""),
                    status=meta.get("status", ""),
                    key_points=meta.get("key_points", ""),
                    section=meta.get("section", ""),
                    source_title=meta.get("source_title", ""),
                    source_ref=meta.get("source_ref", ""),
                    doc_type=meta.get("doc_type", ""),
                    distance=hit.get("distance"),
                )
            )
        return items

    @staticmethod
    def _build_context(items: List[RetrievedItem]) -> str:
        if not items:
            return "（知識庫中查無相關資料）"

        blocks = []
        for idx, it in enumerate(items, start=1):
            source = (
                f"    來源類型：{SOURCE_TYPE_LABELS.get(it.doc_type, it.doc_type or '未知')}\n"
                f"    來源：{it.source_title}"
                + (f"（{it.section}）" if it.section else "")
                + (f" ({it.source_ref})" if it.source_ref else "")
            )
            if it.kind == "chunk":
                body = "\n".join(f"    {line}" for line in it.summary.splitlines())
                blocks.append(f"[{idx}] 原文段落\n{body}\n{source}")
            else:
                blocks.append(
                    f"[{idx}] 功能卡片｜主題：{it.title}\n"
                    f"    說明：{it.summary}\n"
                    f"    狀態：{it.status}\n"
                    + (f"    重點：{it.key_points}\n" if it.key_points else "")
                    + source
                )
        return "\n".join(blocks)

    def _call_llm(self, messages: List[dict]) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=config.LLM_TEMPERATURE,
            )
        except openai.AuthenticationError as exc:
            raise RAGEngineError("RAP API Key 驗證失敗，請確認 RAP_API_KEY 是否正確、是否已過期。") from exc
        except openai.APIConnectionError as exc:
            raise RAGEngineError(f"無法連線至 RAP 端點，請確認網路狀況與端點網址是否正確：{exc}") from exc
        except openai.RateLimitError as exc:
            raise RAGEngineError("RAP LLM 已達速率限制，請稍後再試。") from exc
        except openai.APIStatusError as exc:
            raise RAGEngineError(f"RAP LLM 回應錯誤 (status={exc.status_code})：{exc.message}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RAGEngineError(f"呼叫 RAP LLM 時發生未預期錯誤：{exc}") from exc

        if not response.choices or not response.choices[0].message.content:
            raise RAGEngineError("RAP LLM 回應為空，請稍後再試。")

        return response.choices[0].message.content.strip()

    # ------------------------------------------------------------------
    def answer(self, question: str, history: Sequence[Tuple[str, str]] = ()) -> RAGAnswer:
        """回答問題。

        history：先前的 (使用者問題, 助理回答) 配對，由舊到新；只取最後 MAX_HISTORY_TURNS 輪。
        """
        question = (question or "").strip()
        if not question:
            raise RAGEngineError("提問內容不可為空。")

        recent = list(history)[-MAX_HISTORY_TURNS:]
        # 追問（「那 P3 呢？」）單獨檢索會失去指涉對象，併入上一個問題一起檢索；
        # 本輪問題放前面，embedding 模型超過輸入上限時從尾端截斷
        query = f"{question}\n{recent[-1][0]}" if recent else question
        items = self._retrieve(query)

        messages: List[dict] = [{"role": "system", "content": self._system_prompt}]
        for past_question, past_answer in recent:
            messages.append({"role": "user", "content": past_question})
            messages.append({"role": "assistant", "content": past_answer})
        messages.append(
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(context=self._build_context(items), question=question),
            }
        )

        return RAGAnswer(question=question, markdown=self._call_llm(messages), retrieved=items)
