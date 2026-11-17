"""
Anthropic（Claude）後端：使用官方 Anthropic SDK，不用 OpenAI 相容層
（官方文件說明相容層「not considered a long-term or production-ready solution」）。

- 不送 temperature：目前的 Claude 模型不接受取樣參數。
- 支援 server-side fallback 的模型，預設帶 `fallbacks="default"`：模型因安全分類器拒答時，
  由 API 自動改用合適的模型完成同一個請求。設 LLM_ANTHROPIC_FALLBACKS=off 可關閉。
- stop_reason == "refusal" 時回報錯誤，不把空白當回答。
"""
from __future__ import annotations

import os
from typing import List

from src.llm import LLMError, LLMSettings

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5"}
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "16000"))


class AnthropicBackend:
    def __init__(self, s: LLMSettings) -> None:
        import anthropic

        self.s = s
        kwargs = {"api_key": s.api_key, "timeout": s.timeout}
        if s.base_url:
            kwargs["base_url"] = s.base_url
        self._client = anthropic.Anthropic(**kwargs)
        self._use_fallbacks = (
            s.model in FALLBACK_MODELS and os.getenv("LLM_ANTHROPIC_FALLBACKS", "default").lower() != "off"
        )

    def chat(self, system: str, messages: List[dict]) -> str:
        import anthropic

        try:
            if self._use_fallbacks:
                resp = self._client.beta.messages.create(
                    model=self.s.model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=messages,
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
            else:
                resp = self._client.messages.create(
                    model=self.s.model, max_tokens=MAX_TOKENS, system=system, messages=messages
                )
        except anthropic.AuthenticationError as exc:
            raise LLMError(f"{self.s.label} API key 驗證失敗，請確認 LLM_API_KEY 是否正確、是否過期。") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"{self.s.label} 找不到模型，請確認 LLM_MODEL：{exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"{self.s.label} 已達速率或額度上限，請稍後再試。") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"{self.s.label} 回應錯誤 (status={exc.status_code})：{exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"無法連線到 {self.s.label}，請確認網路。") from exc

        if resp.stop_reason == "refusal":
            raise LLMError(f"{self.s.label} 拒絕回答這個問題（安全政策）。請換個問法或聯絡管理員。")
        return "".join(b.text for b in resp.content if b.type == "text")
