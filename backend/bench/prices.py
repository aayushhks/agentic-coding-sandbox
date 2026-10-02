"""What a model's tokens cost, from a price list pinned in the repo with where each came from."""

import json
from pathlib import Path

from pydantic import BaseModel

PRICES_PATH = Path(__file__).resolve().parent / "prices.json"


class Price(BaseModel):
    model: str
    provider: str
    currency: str
    input_per_million_tokens: float
    output_per_million_tokens: float
    source: str
    retrieved: str
    how: str
    note: str = ""

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.input_per_million_tokens
            + completion_tokens * self.output_per_million_tokens
        ) / 1_000_000


def price_for(model: str, path: Path | None = None) -> Price | None:
    """The pinned price of a model's tokens, or None when the list has no price for it."""
    prices = json.loads((path or PRICES_PATH).read_text())
    entry = prices["models"].get(model)
    if entry is None:
        return None
    return Price(model=model, currency=prices["currency"], **entry)
