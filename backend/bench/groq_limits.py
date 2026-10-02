"""How the recorder treats Groq API errors: wait and retry, or give up."""

import groq

DEFAULT_DELAY_SECONDS = 5.0
MIN_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 120.0


def is_daily_cap(exc: BaseException) -> bool:
    """True when a rate limit is a per-day quota, which only resets the next day."""
    return isinstance(exc, groq.RateLimitError) and "per day" in str(exc).lower()


def provider_message(exc: BaseException, limit: int = 400) -> str:
    """What the provider itself said about an error, which for a cap names the limit and use."""
    body = getattr(exc, "body", None)
    inner = body.get("error", body) if isinstance(body, dict) else None
    message = inner.get("message") if isinstance(inner, dict) else None
    text = message if isinstance(message, str) else str(exc)
    return text[:limit]


def retry_delay(exc: Exception) -> float | None:
    """Seconds to wait before retrying a Groq error, or None when retrying cannot help."""
    if isinstance(exc, groq.RateLimitError):
        return None if is_daily_cap(exc) else _retry_after(exc)
    if isinstance(exc, groq.APIConnectionError | groq.InternalServerError):
        return DEFAULT_DELAY_SECONDS
    return None


def _retry_after(exc: groq.RateLimitError) -> float:
    header = exc.response.headers.get("retry-after")
    try:
        seconds = float(header) if header is not None else DEFAULT_DELAY_SECONDS
    except ValueError:
        seconds = DEFAULT_DELAY_SECONDS
    return min(max(seconds, MIN_DELAY_SECONDS), MAX_DELAY_SECONDS)
