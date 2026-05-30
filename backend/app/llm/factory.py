from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.groq_provider import GroqProvider
from app.llm.mock_provider import MockProvider


def build_provider(settings: Settings) -> LLMProvider:
    """Construct an LLM provider from settings.

    The "mock" branch returns a single canned response so the app is usable without an
    API key; tests construct MockProvider directly with their own scripted responses.
    """
    provider = settings.llm_provider.lower()
    if provider == "groq":
        if settings.groq_api_key is None:
            raise ValueError("groq provider selected but GROQ_API_KEY is not set")
        return GroqProvider(settings.groq_api_key, model=settings.groq_model)
    if provider == "mock":
        return MockProvider(responses=["this is a mock response"])
    raise ValueError(f"unknown llm provider: {settings.llm_provider!r}")
