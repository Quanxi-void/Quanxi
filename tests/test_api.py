from fastapi.testclient import TestClient
from app.main import app


def test_framework_docs_and_openapi_are_disabled_custom_portal_is_available():
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/").json()["documentation"] == "/api-docs"
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs/oauth2-redirect").status_code == 404
        portal = client.get("/api-docs")
        assert portal.status_code == 200
        assert "ManhwaHQ API" in portal.text
        assert "API Playground" in portal.text
        assert app.openapi_url is None


def test_validation_error_uses_standard_envelope():
    with TestClient(app) as client:
        response = client.get("/api/v1/search")
        assert response.status_code == 422
        payload = response.json()
        assert payload["success"] is False
        assert payload["error"]["code"] == "VALIDATION_ERROR"


def test_manga_detail_endpoint_enriches_json_record_with_related_section(monkeypatch):
    from unittest.mock import AsyncMock
    from app.api import routes

    related_html = '''
    <html><body><main>
      <h1>Current Series</h1>
      <section class="mt-8 mb-2">
        <div><h2>Related</h2><a href="/browse?genre=Action">More</a></div>
        <div class="grid">
          <div>
            <a href="/title/recommended-series">
              <div>
                <img src="https://b.toonking.xyz/uploads/covers/recommended_card.webp"
                     alt="Recommended Series" />
                <span class="font-medium tracking-wide leading-none">Manhwa</span>
              </div>
              <div>
                <div class="flex justify-between items-center">
                  <span>Ch. 42</span><span>3d ago</span>
                </div>
                <h3>Recommended Series</h3>
              </div>
            </a>
          </div>
        </div>
      </section>
    </main></body></html>
    '''

    async def fake_source_data(params):
        return (
            {"id": "current-series", "slug": "current-series", "title": "Current Series"},
            {"success": True},
            "https://b.toonking.xyz/api/local-manga?id=current-series",
            False,
        )

    monkeypatch.setattr(routes, "fetch_source_data", fake_source_data)
    monkeypatch.setattr(
        routes,
        "fetch_or_502",
        AsyncMock(return_value=(related_html, "https://manhwahq.com/title/current-series", False)),
    )

    with TestClient(routes.app if hasattr(routes, "app") else __import__("app.main", fromlist=["app"]).app) as client:
        response = client.get("/api/v1/manga/current-series")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert len(payload["recommendations"]) == 1
    card = payload["recommendations"][0]
    assert card["slug"] == "recommended-series"
    assert card["title"] == "Recommended Series"
    assert card["type"] == "Manhwa"
    assert card["latest_chapter"] == "Ch. 42"
    assert card["updated_at"] == "3d ago"


def test_recommendations_endpoint_returns_only_related_cards(monkeypatch):
    from unittest.mock import AsyncMock
    from app.api import routes
    from app.main import app

    related_html = '''
    <html><body><main>
      <h1>Current Series</h1>
      <section class="mt-8 mb-2">
        <h2>Related</h2>
        <a href="/title/recommended-series">
          <img src="https://b.toonking.xyz/recommended.webp" alt="Recommended Series" />
          <div class="flex justify-between items-center"><span>Ch. 42</span><span>3d ago</span></div>
          <span class="font-medium tracking-wide leading-none">Manhwa</span>
          <h3>Recommended Series</h3>
        </a>
      </section>
    </main></body></html>
    '''
    monkeypatch.setattr(
        routes,
        "fetch_or_502",
        AsyncMock(return_value=(related_html, "https://manhwahq.com/title/current-series", False)),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/manga/current-series/recommendations")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["manga_slug"] == "current-series"
    assert len(data["items"]) == 1
    assert data["items"][0]["slug"] == "recommended-series"
    assert data["items"][0]["type"] == "Manhwa"


def test_account_routes_still_exist_but_are_not_in_public_playground():
    from pathlib import Path
    docs_js = Path(__file__).resolve().parents[1] / "app" / "static" / "developer_docs" / "app.js"
    public_portal_source = docs_js.read_text(encoding="utf-8")
    assert "/api/v1/auth/" not in public_portal_source
    assert "/api/v1/users/" not in public_portal_source
    registered_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/auth/login" in registered_paths
    assert "/api/v1/users/me" in registered_paths


def test_custom_documentation_assets_are_served():
    with TestClient(app) as client:
        css = client.get("/api-docs/assets/styles.css")
        js = client.get("/api-docs/assets/app.js")
    assert css.status_code == 200
    assert "sidebar" in css.text
    assert js.status_code == 200
    assert "Chapter reader" in js.text


def test_builtin_avatar_catalog_is_served():
    with TestClient(app) as client:
        response = client.get("/api/v1/users/avatars")
        assert response.status_code == 200
        items = response.json()["data"]["items"]
        assert len(items) >= 20
        image = client.get(items[0]["url"])
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/")


def test_password_hashing_uses_one_way_hash():
    from app.auth.security import hash_password, verify_password
    password = "Correct-Horse-Battery-42!"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed)
    assert not verify_password("wrong-password", hashed)
