"""
AI 模型（LLM）的統一入口：讓每家導入企業用自己選的模型驅動大腦。

設定（.env）：
    LLM_PROVIDER   rap | openai | anthropic | openrouter | gemini | ollama | custom
    LLM_API_KEY    該服務的 API key（ollama 可留空）
    LLM_MODEL      模型名稱，例如 gpt-5.1、claude-opus-5-5、Llama-3.3-70B-Instruct
    LLM_BASE_URL   選填；custom 必填。其他服務有預設端點，填了就覆蓋（例如 Azure OpenAI、自架 vLLM）

相容舊設定：沒有 LLM_PROVIDER、但有 RAP_API_KEY 時，視為 provider=rap，沿用 RAP_BASE_URL／RAP_MODEL_NAME。
不設定任何預設模型：導入時必須明確選一家（安裝精靈 `python -m src.setup_wizard` 會問）。

anthropic 走官方 Anthropic SDK（src/llm_anthropic.py），其餘都走 OpenAI 相容格式（openai SDK）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from src import config

logger = config.get_logger(__name__)


class LLMError(Exception):
    """呼叫模型失敗；訊息可直接給管理員看（不含 API key）。"""


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str  # 空字串＝不適用（anthropic）或必須由使用者填（custom）
    needs_key: bool = True
    default_temperature: Optional[float] = None
    signup: str = ""


PROVIDERS: Dict[str, Provider] = {
    p.key: p
    for p in [
        Provider("rap", "國網中心 AI RAP（需為 RAP 計畫成員；條款限制商業使用，請自行確認）",
                 "https://portal.genai.nchc.org.tw/api/v1", default_temperature=0.2,
                 signup="https://portal.genai.nchc.org.tw"),
        Provider("openai", "OpenAI（GPT）", "https://api.openai.com/v1",
                 signup="https://platform.openai.com/api-keys"),
        Provider("anthropic", "Anthropic（Claude）", "",
                 signup="https://platform.claude.com/settings/keys"),
        Provider("openrouter", "OpenRouter（一把 key 用多家模型）", "https://openrouter.ai/api/v1",
                 signup="https://openrouter.ai/keys"),
        Provider("gemini", "Google Gemini（OpenAI 相容端點）",
                 "https://generativelanguage.googleapis.com/v1beta/openai/",
                 signup="https://aistudio.google.com/apikey"),
        Provider("ollama", "Ollama（地端模型，資料不出公司）", "http://localhost:11434/v1", needs_key=False),
        Provider("custom", "其他 OpenAI 相容端點（vLLM、Azure OpenAI、LiteLLM…）", "", needs_key=False),
    ]
}


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    temperature: Optional[float] = None
    timeout: float = 60.0

    @property
    def label(self) -> str:
        p = PROVIDERS.get(self.provider)
        return f"{p.label if p else self.provider}／{self.model or '（未設定模型）'}"


def _env_float(name: str) -> Optional[float]:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else None


def load_settings(env: Optional[Dict[str, str]] = None) -> LLMSettings:
    """從環境變數讀設定；env 參數供測試注入。"""
    get = (env if env is not None else os.environ).get
    provider = (get("LLM_PROVIDER") or "").strip().lower()
    api_key = (get("LLM_API_KEY") or "").strip()
    model = (get("LLM_MODEL") or "").strip()
    base_url = (get("LLM_BASE_URL") or "").strip()

    if not provider and (get("RAP_API_KEY") or "").strip():
        # 舊版只有 RAP_* 設定
        provider = "rap"
        api_key = api_key or get("RAP_API_KEY", "").strip()
        model = model or (get("RAP_MODEL_NAME") or "").strip()
        base_url = base_url or (get("RAP_BASE_URL") or "").strip()

    preset = PROVIDERS.get(provider)
    if preset and not base_url:
        base_url = preset.base_url
    if provider == "ollama" and not api_key:
        api_key = "ollama"  # Ollama 需要非空字串但不檢查

    raw_temp = (get("LLM_TEMPERATURE") or "").strip()
    temperature = float(raw_temp) if raw_temp else (preset.default_temperature if preset else None)
    timeout = float((get("LLM_REQUEST_TIMEOUT") or "60").strip() or 60)
    return LLMSettings(provider, model, api_key, base_url, temperature, timeout)


def missing_settings(s: Optional[LLMSettings] = None) -> List[str]:
    s = s or load_settings()
    if not s.provider:
        return [
            "AI 模型未設定：請在 .env 設定 LLM_PROVIDER（"
            + "／".join(PROVIDERS)
            + "）、LLM_API_KEY、LLM_MODEL，或執行 `python -m src.setup_wizard`。"
        ]
    if s.provider not in PROVIDERS:
        return [f"LLM_PROVIDER={s.provider} 不支援；可用：{'、'.join(PROVIDERS)}。"]
    out = []
    if PROVIDERS[s.provider].needs_key and not s.api_key:
        out.append(f"LLM_API_KEY 未設定：請填入 {PROVIDERS[s.provider].label} 的 API key"
                   + (f"（申請：{PROVIDERS[s.provider].signup}）" if PROVIDERS[s.provider].signup else "") + "。")
    if not s.model:
        out.append("LLM_MODEL 未設定：請填入要使用的模型名稱。")
    if s.provider == "custom" and not s.base_url:
        out.append("LLM_BASE_URL 未設定：provider=custom 時必須填入 OpenAI 相容端點網址。")
    return out


class LLMClient:
    """chat(system, messages) -> 回答文字。messages 只含 user／assistant 兩種角色。"""

    def __init__(self, settings: Optional[LLMSettings] = None) -> None:
        self.settings = settings or load_settings()
        problems = missing_settings(self.settings)
        if problems:
            raise LLMError(" ".join(problems))
        if self.settings.provider == "anthropic":
            from src.llm_anthropic import AnthropicBackend

            self._backend = AnthropicBackend(self.settings)
        else:
            self._backend = _OpenAICompatibleBackend(self.settings)

    @property
    def label(self) -> str:
        return self.settings.label

    def chat(self, system: str, messages: List[dict]) -> str:
        text = self._backend.chat(system, messages)
        if not text or not text.strip():
            raise LLMError(f"{self.label} 回應為空，請稍後再試。")
        return text.strip()


class _OpenAICompatibleBackend:
    def __init__(self, s: LLMSettings) -> None:
        from openai import OpenAI

        self.s = s
        try:
            self._client = OpenAI(api_key=s.api_key or "unused", base_url=s.base_url, timeout=s.timeout)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"初始化 {s.label} 客戶端失敗：{exc}") from exc

    def chat(self, system: str, messages: List[dict]) -> str:
        import openai

        kwargs = {}
        if self.s.temperature is not None:
            kwargs["temperature"] = self.s.temperature
        try:
            resp = self._client.chat.completions.create(
                model=self.s.model,
                messages=[{"role": "system", "content": system}, *messages],
                **kwargs,
            )
        except openai.AuthenticationError as exc:
            raise LLMError(f"{self.s.label} API key 驗證失敗，請確認 LLM_API_KEY 是否正確、是否過期。") from exc
        except openai.NotFoundError as exc:
            raise LLMError(f"{self.s.label} 找不到模型或端點，請確認 LLM_MODEL／LLM_BASE_URL：{exc.message}") from exc
        except openai.RateLimitError as exc:
            raise LLMError(f"{self.s.label} 已達速率或額度上限，請稍後再試。") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"{self.s.label} 回應錯誤 (status={exc.status_code})：{exc.message}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"無法連線到 {self.s.label}（{self.s.base_url}），請確認網路與端點網址。") from exc
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"呼叫 {self.s.label} 時發生未預期錯誤：{exc}") from exc
        if not resp.choices:
            return ""
        return resp.choices[0].message.content or ""
