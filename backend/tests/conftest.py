from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient that runs the app's lifespan (startup and shutdown)."""
    with TestClient(create_app()) as test_client:
        yield test_client
