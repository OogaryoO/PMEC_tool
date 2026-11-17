"""G7：客戶自選 AI 模型。不連外，用假的 SDK 客戶端檢查送出的參數。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import llm
from src.llm import LLMClient, LLMError, LLMSettings, load_settings, missing_settings


def test_no_default_provider():
    s = load_settings({})
    assert s.provider == ""
    assert "AI 模型未設定" in missing_settings(s)[0]
    with pytest.raises(LLMError):
        LLMClient(s)


def test_legacy_rap_env_still_works():
    s = load_settings({"RAP_API_KEY": "k", "RAP_MODEL_NAME": "Llama-3.3-70B-Instruct"})
    assert s.provider == "rap"
    assert s.base_url.startswith("https://portal.genai.nchc.org.tw")
    assert s.temperature == 0.2
    assert missing_settings(s) == []


@pytest.mark.parametrize(
    "provider,expected_url",
    [
        ("openai", "https://api.openai.com/v1"),
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        ("ollama", "http://localhost:11434/v1"),
    ],
)
def test_presets(provider, expected_url):
    s = load_settings({"LLM_PROVIDER": provider, "LLM_API_KEY": "k", "LLM_MODEL": "m"})
    assert s.base_url == expected_url
    assert s.temperature is None  # 只有 RAP 預設送 temperature
    assert missing_settings(s) == []


def test_ollama_needs_no_key_and_custom_needs_url():
    assert missing_settings(load_settings({"LLM_PROVIDER": "ollama", "LLM_MODEL": "llama3.3"})) == []
    problems = missing_settings(load_settings({"LLM_PROVIDER": "custom", "LLM_MODEL": "m"}))
    assert any("LLM_BASE_URL" in p for p in problems)
    assert missing_settings(load_settings({"LLM_PROVIDER": "nope", "LLM_MODEL": "m"}))[0].startswith("LLM_PROVIDER=nope")


def test_base_url_override_for_preset():
    s = load_settings({"LLM_PROVIDER": "openai", "LLM_API_KEY": "k", "LLM_MODEL": "m",
                       "LLM_BASE_URL": "https://my.azure.example/openai/v1"})
    assert s.base_url == "https://my.azure.example/openai/v1"


class _FakeOpenAI:
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeOpenAI.last = self
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" 答案 "))])


def test_openai_compatible_backend_sends_system_and_omits_temperature(monkeypatch):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    client = LLMClient(LLMSettings("openai", "gpt-x", "k", "https://api.openai.com/v1"))
    assert client.chat("SYS", [{"role": "user", "content": "Q"}]) == "答案"
    req = _FakeOpenAI.last.request
    assert req["messages"][0] == {"role": "system", "content": "SYS"}
    assert "temperature" not in req


class _FakeAnthropic:
    last = None
    stop_reason = "end_turn"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeAnthropic.last = self
        self.messages = SimpleNamespace(create=lambda **kw: self._create("plain", kw))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self._create("beta", kw)))

    def _create(self, kind, kw):
        self.kind, self.request = kind, kw
        return SimpleNamespace(
            stop_reason=_FakeAnthropic.stop_reason,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="Claude 答")],
        )


def test_anthropic_backend_uses_sdk_with_fallbacks_and_no_temperature(monkeypatch):
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    _FakeAnthropic.stop_reason = "end_turn"
    client = LLMClient(LLMSettings("anthropic", "claude-opus-5-5", "k", temperature=0.2))
    assert client.chat("SYS", [{"role": "user", "content": "Q"}]) == "Claude 答"
    f = _FakeAnthropic.last
    assert f.kind == "beta"
    assert f.request["fallbacks"] == "default"
    assert f.request["system"] == "SYS"
    assert "temperature" not in f.request
    assert f.request["max_tokens"] >= 1024


def test_anthropic_other_model_without_fallbacks_and_refusal(monkeypatch):
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    client = LLMClient(LLMSettings("anthropic", "claude-haiku-5-5", "k"))
    _FakeAnthropic.stop_reason = "end_turn"
    client.chat("S", [{"role": "user", "content": "Q"}])
    assert _FakeAnthropic.last.kind == "plain"
    _FakeAnthropic.stop_reason = "refusal"
    with pytest.raises(LLMError, match="拒絕"):
        client.chat("S", [{"role": "user", "content": "Q"}])
    _FakeAnthropic.stop_reason = "end_turn"


def test_all_providers_have_labels():
    for key, p in llm.PROVIDERS.items():
        assert p.key == key and p.label
