import groq
import httpx

from bench.groq_limits import (
    DEFAULT_DELAY_SECONDS,
    MAX_DELAY_SECONDS,
    is_daily_cap,
    provider_message,
    retry_delay,
)

_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def _status_error(
    cls: type[groq.APIStatusError], status: int, message: str, headers: dict[str, str] | None = None
) -> groq.APIStatusError:
    response = httpx.Response(status, headers=headers or {}, request=_REQUEST)
    return cls(message, response=response, body=None)


_PER_MINUTE = "Rate limit reached for model on tokens per minute (TPM): Limit 8000, Used 7900"
_PER_DAY = "Rate limit reached for model on tokens per day (TPD): Limit 500000, Used 499000"


def test_per_minute_limit_waits_the_retry_after_header() -> None:
    exc = _status_error(groq.RateLimitError, 429, _PER_MINUTE, {"retry-after": "3"})
    assert retry_delay(exc) == 3.0
    assert not is_daily_cap(exc)


def test_per_minute_limit_without_a_header_uses_the_default() -> None:
    exc = _status_error(groq.RateLimitError, 429, _PER_MINUTE)
    assert retry_delay(exc) == DEFAULT_DELAY_SECONDS


def test_retry_after_is_clamped() -> None:
    huge = _status_error(groq.RateLimitError, 429, _PER_MINUTE, {"retry-after": "9999"})
    zero = _status_error(groq.RateLimitError, 429, _PER_MINUTE, {"retry-after": "0"})
    assert retry_delay(huge) == MAX_DELAY_SECONDS
    assert retry_delay(zero) == 1.0


def test_daily_cap_is_not_retried() -> None:
    exc = _status_error(groq.RateLimitError, 429, _PER_DAY, {"retry-after": "600"})
    assert is_daily_cap(exc)
    assert retry_delay(exc) is None


def test_transient_failures_are_retried() -> None:
    assert retry_delay(_status_error(groq.InternalServerError, 503, "unavailable")) == 5.0
    assert retry_delay(groq.APIConnectionError(request=_REQUEST)) == 5.0
    assert retry_delay(groq.APITimeoutError(request=_REQUEST)) == 5.0


def test_request_errors_are_not_retried() -> None:
    assert retry_delay(_status_error(groq.BadRequestError, 400, "bad request")) is None
    assert retry_delay(_status_error(groq.APIStatusError, 413, "request too large")) is None
    assert retry_delay(ValueError("not a groq error")) is None


def test_the_provider_s_own_message_is_kept_from_the_body_it_sent() -> None:
    said = (
        "Rate limit reached ... on tokens per day (TPD): Limit 200000, Used 199523, Requested 2513"
    )
    response = httpx.Response(429, request=_REQUEST)
    body = {"error": {"message": said, "type": "tokens", "code": "rate_limit_exceeded"}}
    exc = groq.RateLimitError(f"Error code: 429 - {body}", response=response, body=body)
    assert provider_message(exc) == said
    # without a body, the error's own text stands in, cut to a length a record can hold
    bare = _status_error(groq.RateLimitError, 429, "x" * 1000)
    assert provider_message(bare) == "x" * 400


def test_the_provider_s_message_leaves_out_the_account_it_names() -> None:
    said = (
        "Rate limit reached for model `m` in organization `org_01k94fjezdecqv8v01awf25mmc` on TPD"
    )
    response = httpx.Response(429, request=_REQUEST)
    body = {"error": {"message": said}}
    exc = groq.RateLimitError(f"Error code: 429 - {body}", response=response, body=body)
    assert provider_message(exc) == (
        "Rate limit reached for model `m` in organization `org_[redacted]` on TPD"
    )
