"""
RAG 引擎：接收 PM 的商業提問 -> 向量檢索相關功能卡片 -> 呼叫 RAP LLM
產出「可直接用於對客戶溝通」的結構化回答。

輸出固定包含四個區塊：
    【能否承諾客戶】：可以 / 有條件可以 / 目前無法
    【現況支援程度】：非技術語言說明
    【差距與風險（Gap Analysis）】：規格落差與技術限制
    【建議對外溝通說法】：可直接對客戶發送的說法
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import openai
from openai import OpenAI

from src import config
from src.vector_store import VectorStore, VectorStoreError

logger = config.get_logger(__name__)


class RAGEngineError(Exception):
    """檢索或生成回答失敗時拋出，訊息需可直接呈現給使用者。"""


SYSTEM_PROMPT = (
    "你是內部的「PM ↔ 工程對齊」助理。你的任務是根據『已檢索到的實際實作紀錄』，"
    "誠實評估目前系統是否能滿足 PM 或業務提出的客戶需求，禁止杜撰未在檢索資料中出現的功能。"
    "若檢索資料不足以判斷，必須明確說明資訊不足，而不是臆測。"
)

USER_PROMPT_TEMPLATE = """\
PM / 業務提出的商業問題：
「{question}」

以下是從 GitHub 知識庫（{repo_name}）檢索到的相關實作摘要（依相關度排序，可能包含不完全相關的結果）：
---
{context}
---

請根據上述資料，嚴格以下列格式輸出繁體中文回答（保留【】標題文字，不要增加其他標題，不要使用程式碼區塊）：

【能否承諾客戶】：（僅能填寫「可以」、「有條件可以」或「目前無法」三者之一，並附上一句話理由）
【現況支援程度】：（用非技術語言，向 PM 說明目前系統實際做到什麼程度，2-4 句話）
【差距與風險（Gap Analysis）】：（條列說明規格落差、技術限制、尚未驗證的部分；若無明顯落差請說明「無明顯落差」）
【建議對外溝通說法】：（提供一段可以直接複製貼上、對客戶說的中立說法，避免過度承諾）

注意：
1. 若檢索到的資料與問題明顯無關或筆數為 0，【能否承諾客戶】必須填寫「目前無法」，並在其餘欄位說明「知識庫中尚無相關實作紀錄，需工程團隊確認」。
2. 禁止引用檢索資料以外的功能或承諾。
"""

SECTION_HEADERS = [
    "能否承諾客戶",
    "現況支援程度",
    "差距與風險（Gap Analysis）",
    "建議對外溝通說法",
]


@dataclass
class RetrievedCard:
    feature_name: str
    business_scenario: str
    status: str
    source_title: str
    source_ref: str
    distance: Optional[float]


@dataclass
class RAGAnswer:
    question: str
    raw_markdown: str
    sections: Dict[str, str] = field(default_factory=dict)
    retrieved: List[RetrievedCard] = field(default_factory=list)


class RAGEngine:
    """組合 VectorStore 檢索與 RAP LLM 生成，產出結構化商務對齊回答。"""

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

    # ------------------------------------------------------------------
    def _retrieve(self, question: str) -> List[RetrievedCard]:
        try:
            hits = self.vector_store.query(question, top_k=self.top_k)
        except VectorStoreError as exc:
            raise RAGEngineError(str(exc)) from exc

        cards: List[RetrievedCard] = []
        for hit in hits:
            meta = hit.get("metadata", {}) or {}
            cards.append(
                RetrievedCard(
                    feature_name=meta.get("feature_name", "(未知功能)"),
                    business_scenario=meta.get("business_scenario", ""),
                    status=meta.get("status", ""),
                    source_title=meta.get("source_title", ""),
                    source_ref=meta.get("source_ref", ""),
                    distance=hit.get("distance"),
                )
            )
        return cards

    @staticmethod
    def _build_context(cards: List[RetrievedCard]) -> str:
        if not cards:
            return "（知識庫中查無相關資料）"

        blocks = []
        for idx, c in enumerate(cards, start=1):
            blocks.append(
                f"{idx}. 功能名稱：{c.feature_name}\n"
                f"   商業場景支援：{c.business_scenario}\n"
                f"   目前狀態：{c.status}\n"
                f"   來源：{c.source_title}"
                + (f" ({c.source_ref})" if c.source_ref else "")
            )
        return "\n".join(blocks)

    def _call_llm(self, question: str, context: str) -> str:
        prompt = USER_PROMPT_TEMPLATE.format(question=question, repo_name=self.repo_name, context=context)
        try:
            response = self._client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
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

    @staticmethod
    def _parse_sections(raw_text: str) -> Dict[str, str]:
        """把「【標題】：內容」格式的區塊切出來，找不到就整段放進最相近的鍵。"""
        pattern = "|".join(re.escape(h) for h in SECTION_HEADERS)
        splitter = re.compile(rf"【({pattern})】[：:]?")

        matches = list(splitter.finditer(raw_text))
        sections: Dict[str, str] = {}

        if not matches:
            return sections

        for i, m in enumerate(matches):
            key = m.group(1)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
            sections[key] = raw_text[start:end].strip(" \n：:")

        return sections

    # ------------------------------------------------------------------
    def answer(self, question: str) -> RAGAnswer:
        if not question or not question.strip():
            raise RAGEngineError("提問內容不可為空。")

        cards = self._retrieve(question)
        context = self._build_context(cards)
        raw_markdown = self._call_llm(question, context)
        sections = self._parse_sections(raw_markdown)

        return RAGAnswer(
            question=question,
            raw_markdown=raw_markdown,
            sections=sections,
            retrieved=cards,
        )
