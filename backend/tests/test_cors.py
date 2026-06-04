from fastapi.testclient import TestClient


def test_cors_header_present_for_cross_origin_request(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "https://dashboard.example"})
    assert response.status_code == 200
    # default cors_origins is "*", so the API is callable from any origin (e.g. the Vercel SPA)
    assert response.headers.get("access-control-allow-origin") == "*"
