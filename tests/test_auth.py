"""Unified web login (tp-azure gateway): guard, callback, session lifecycle.

Offline tests: the JWT is signed locally with the configured secret, no real
auth server or network needed.
"""

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, Settings
from app.main import create_app
from app.storage import Repository

SECRET = "test-azure-secret-0123456789abcdef"


def auth_settings(**overrides) -> AuthSettings:
    defaults = dict(
        enabled=True,
        jwt_secret=SECRET,
        session_secret="test-session-secret",
    )
    defaults.update(overrides)
    return AuthSettings(**defaults)


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        auth=auth_settings(),
    )
    app = create_app(settings, repository=Repository(settings.database_path))
    return TestClient(app)


def make_token(exp_offset_seconds: int = 300, **overrides) -> str:
    payload = {
        "user_id": "u-1",
        "name": "测试用户",
        "email": "tester@example.com",
        "exp": int(time.time()) + exp_offset_seconds,
    }
    payload.update(overrides)
    return jwt.encode(payload, SECRET, algorithm="HS256")


# -- guard --------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/workbench", "/templates"])
def test_unauthenticated_pages_redirect_to_login(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_api_returns_401(client):
    response = client.get("/api/sessions")
    assert response.status_code == 401


def test_login_page_and_static_are_public(client):
    response = client.get("/login")
    assert response.status_code == 200
    # Unified-auth entry point points at the tp-azure gateway.
    assert "https://cluster.tpcnailab.com/login/e2etrans" in response.text
    assert client.get("/static/styles.css").status_code == 200


def test_login_page_shows_error_message(client):
    response = client.get("/login?error=expired")
    assert "认证令牌已过期" in response.text


# -- verify callback -----------------------------------------------------------


def test_verify_callback_sets_session_and_redirects_home(client):
    response = client.get(
        f"/auth/verify?jwt={make_token()}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    # Session cookie now grants access to pages and API.
    assert client.get("/").status_code == 200
    assert client.get("/api/sessions").status_code == 200
    user = client.get("/api/auth/user").json()
    assert user["authenticated"] is True
    assert user["user_info"]["email"] == "tester@example.com"


def test_verify_callback_without_token(client):
    response = client.get("/auth/verify", follow_redirects=False)
    assert response.headers["location"] == "/login?error=missing_token"


def test_verify_callback_rejects_bad_signature(client):
    token = jwt.encode(
        {"user_id": "u-1", "name": "x", "email": "x@x.com", "exp": int(time.time()) + 60},
        "wrong-secret-0123456789abcdef0123456789",
        algorithm="HS256",
    )
    response = client.get(f"/auth/verify?jwt={token}", follow_redirects=False)
    assert response.headers["location"] == "/login?error=invalid"


def test_verify_callback_rejects_expired_token(client):
    response = client.get(
        f"/auth/verify?jwt={make_token(exp_offset_seconds=-60)}",
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login?error=expired"


def test_verify_callback_rejects_incomplete_claims(client):
    token = make_token(email=None)
    response = client.get(f"/auth/verify?jwt={token}", follow_redirects=False)
    assert response.headers["location"] == "/login?error=invalid"


def test_verify_json_api_endpoint(client):
    ok = client.get(f"/api/auth/verify?jwt={make_token()}")
    assert ok.status_code == 200
    assert ok.json()["success"] is True
    assert client.get("/api/auth/user").json()["authenticated"] is True

    missing = client.get("/api/auth/verify")
    assert missing.status_code == 400
    bad = client.get("/api/auth/verify?jwt=not-a-jwt")
    assert bad.status_code == 401


# -- logout --------------------------------------------------------------------


def test_logout_clears_session(client):
    client.get(f"/auth/verify?jwt={make_token()}")
    assert client.get("/", follow_redirects=False).status_code == 200

    response = client.get("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert "cluster.tpcnailab.com/auth/azure-logout" in response.headers["location"]

    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/auth/user").json()["authenticated"] is False


def test_logout_api_returns_azure_logout_url(client):
    client.get(f"/auth/verify?jwt={make_token()}")
    response = client.post("/api/auth/logout")
    body = response.json()
    assert body["success"] is True
    assert "cluster.tpcnailab.com/auth/azure-logout" in body["azure_logout_url"]
    assert client.get("/api/auth/user").json()["authenticated"] is False


# -- base path (deployed under nginx /v2) --------------------------------------


def test_base_path_prefixes_redirects_and_pages(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        base_path="/v2",
        auth=auth_settings(frontend_url="https://obbot.tpcnailab.com/v2"),
    )
    app = create_app(settings, repository=Repository(settings.database_path))
    client = TestClient(app)

    # Internal routes stay at the root (nginx strips /v2); emitted URLs are prefixed.
    response = client.get("/workbench", follow_redirects=False)
    assert response.headers["location"] == "/v2/login"

    response = client.get(f"/auth/verify?jwt={make_token()}", follow_redirects=False)
    assert response.headers["location"] == "/v2/"

    page = client.get("/")
    assert page.status_code == 200
    assert 'window.APP_BASE="/v2"' in page.text

    logout = client.get("/logout", follow_redirects=False)
    assert "obbot.tpcnailab.com%2Fv2%2Flogin" in logout.headers["location"]
