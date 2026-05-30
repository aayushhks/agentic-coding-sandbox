import pytest

from app.core.config import Settings
from app.llm.base import CompletionResult, LLMProvider, Message, Role
from app.llm.factory import build_provider
from app.llm.groq_provider import GroqProvider
from app.llm.mock_provider import MockProvider


async def test_mock_provider_replays_scripted_responses() -> None:
    provider = MockProvider(responses=["first", "second"])
    messages = [Message(role=Role.USER, content="hello world")]

    first = await provider.complete(messages)
    assert isinstance(first, CompletionResult)
    assert first.content == "first"
    assert first.prompt_tokens == 2
    assert first.completion_tokens == 1
    assert first.total_tokens == 3

    second = await provider.complete(messages)
    assert second.content == "second"
    assert provider.calls == 2


async def test_mock_provider_raises_when_exhausted() -> None:
    provider = MockProvider(responses=["only"])
    await provider.complete([Message(role=Role.USER, content="x")])
    with pytest.raises(IndexError):
        await provider.complete([Message(role=Role.USER, content="x")])


def test_mock_provider_satisfies_interface() -> None:
    provider = MockProvider(responses=[])
    assert isinstance(provider, LLMProvider)
    assert provider.name == "mock"


def test_groq_provider_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api key"):
        GroqProvider(api_key="")


def test_groq_provider_exposes_name_and_model() -> None:
    provider = GroqProvider(api_key="test-key", model="llama-3.3-70b-versatile")
    assert provider.name == "groq"
    assert provider.model == "llama-3.3-70b-versatile"


def test_factory_builds_groq_when_configured() -> None:
    settings = Settings(llm_provider="groq", groq_api_key="test-key")
    assert isinstance(build_provider(settings), GroqProvider)


def test_factory_raises_when_groq_key_missing() -> None:
    settings = Settings(llm_provider="groq", groq_api_key=None)
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        build_provider(settings)


def test_factory_builds_mock_by_default() -> None:
    settings = Settings(llm_provider="mock")
    assert isinstance(build_provider(settings), MockProvider)


def test_factory_rejects_unknown_provider() -> None:
    settings = Settings(llm_provider="does-not-exist")
    with pytest.raises(ValueError, match="unknown"):
        build_provider(settings)
