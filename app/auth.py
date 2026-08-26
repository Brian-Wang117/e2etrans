"""Azure AD unified web login via the tp-azure gateway (cluster.tpcnailab.com).

Flow (mirrors voice-dialogue-bot's AZURE_AUTH_MIGRATION):

    browser -> {AUTH_SERVER}/login/{APP_NAME} -> Azure AD OAuth
    -> auth server signs a JWT and redirects back to
       {external base}/auth/verify?jwt=...
    -> this app verifies the JWT (HS256, AZURE_JWT_SECRET), sets a session
       cookie and redirects to the main page.

The app itself is mounted at the URL root; when deployed behind a prefix
(e.g. nginx serving it at https://obbot.tpcnailab.com/v2), set BASE_PATH=/v2
so emitted redirects/links point at the external URLs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

import jwt as pyjwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from app.config import AuthSettings

logger = logging.getLogger(__name__)

SESSION_MAX_AGE_SECONDS = 86400

_LOGIN_ERRORS = {
    "missing_token": "认证回调缺少令牌，请重新登录",
    "expired": "认证令牌已过期，请重新登录",
    "invalid": "认证令牌无效，请重新登录",
    "incomplete": "认证令牌缺少用户信息，请重新登录",
}


def _public_paths(base: str) -> tuple[set[str], tuple[str, ...]]:
    exact = {
        f"{base}/login",
        f"{base}/auth/verify",
        f"{base}/logout",
    }
    prefixes = (
        f"{base}/static/",
        f"{base}/api/auth/",
        f"{base}/ws/",
    )
    return exact, prefixes


def _verify_token(token: str, settings: AuthSettings) -> dict[str, object]:
    """Decode the auth-server JWT and return its user info, or raise."""
    payload = pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    user_info = {
        "user_id": payload.get("user_id"),
        "name": payload.get("name"),
        "email": payload.get("email"),
    }
    if not all(user_info.values()):
        raise ValueError("incomplete")
    return user_info


def _login_response(static_dir: Path, login_url: str, error: str | None) -> Response:
    html = (static_dir / "login.html").read_text(encoding="utf-8")
    error_html = ""
    if error and error in _LOGIN_ERRORS:
        error_html = f'<p class="error">{_LOGIN_ERRORS[error]}</p>'
    html = html.replace("{{login_url}}", login_url).replace("{{error_html}}", error_html)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def install_auth(
    app: FastAPI,
    settings: AuthSettings,
    base: str,
    static_dir: Path,
    fallback_frontend_url: str,
) -> None:
    """Wire session login into the app. ``base`` is the external URL prefix
    ("" when served at root); ``fallback_frontend_url`` is used for the Azure
    logout redirect when FRONTEND_URL is unset."""
    login_url = f"{settings.auth_server}/login/{settings.app_name}"
    frontend_url = settings.frontend_url or fallback_frontend_url.rstrip("/")
    azure_logout_url = (
        f"{settings.auth_server}/auth/azure-logout"
        f"?post_logout_redirect_uri={quote(frontend_url + '/login', safe='')}"
    )

    # Register the guard first, then the session middleware, so the session
    # middleware ends up outermost and request.session is populated here.
    # The guard compares INTERNAL paths (the reverse proxy strips the base
    # prefix before requests reach the app); only emitted URLs carry `base`.
    public_exact, public_prefixes = _public_paths("")

    @app.middleware("http")
    async def auth_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not request.session.get("authenticated"):
            if path in public_exact or path.startswith(public_prefixes):
                return await call_next(request)
            if path == "/api" or path.startswith("/api/"):
                return JSONResponse({"detail": "未认证，请先登录"}, status_code=401)
            return RedirectResponse(f"{base}/login", status_code=303)
        return await call_next(request)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=settings.session_cookie,
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    # -- pages ----------------------------------------------------------------
    # Routes live at the internal root; `base` only prefixes emitted URLs.

    @app.get("/login")
    async def login_page(request: Request) -> Response:
        if request.session.get("authenticated"):
            return RedirectResponse(f"{base}/", status_code=303)
        error = request.query_params.get("error")
        return _login_response(static_dir, login_url, error)

    @app.get("/auth/verify")
    async def auth_verify(request: Request) -> Response:
        """Auth-server callback: {external}/auth/verify?jwt=..."""
        token = request.query_params.get("jwt")
        if not token:
            return RedirectResponse(f"{base}/login?error=missing_token", status_code=303)
        try:
            user_info = _verify_token(token, settings)
        except pyjwt.ExpiredSignatureError:
            return RedirectResponse(f"{base}/login?error=expired", status_code=303)
        except (pyjwt.InvalidTokenError, ValueError):
            return RedirectResponse(f"{base}/login?error=invalid", status_code=303)
        request.session["authenticated"] = True
        request.session["user_info"] = user_info
        logger.info("user logged in via tp-azure: %s", user_info.get("name"))
        return RedirectResponse(f"{base}/", status_code=303)

    @app.get("/logout")
    async def logout_page(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(azure_logout_url, status_code=303)

    # -- auth API (also reachable without a session) ---------------------------

    @app.get("/api/auth/status")
    async def auth_status() -> dict[str, object]:
        return {
            "unified_auth_enabled": settings.enabled,
            "auth_server": settings.auth_server,
            "app_name": settings.app_name,
        }

    @app.get("/api/auth/user")
    async def auth_user(request: Request) -> dict[str, object]:
        if request.session.get("authenticated"):
            return {
                "authenticated": True,
                "user_info": request.session.get("user_info", {}),
            }
        return {"authenticated": False}

    @app.get("/api/auth/verify")
    async def auth_verify_api(request: Request) -> Response:
        """JSON variant of the callback (kept for parity with the migration doc)."""
        token = request.query_params.get("jwt")
        if not token:
            return JSONResponse({"success": False, "message": "缺少认证令牌"}, status_code=400)
        try:
            user_info = _verify_token(token, settings)
        except pyjwt.ExpiredSignatureError:
            return JSONResponse(
                {"success": False, "message": "认证令牌已过期，请重新登录"}, status_code=401
            )
        except pyjwt.InvalidTokenError as error:
            return JSONResponse(
                {"success": False, "message": f"无效的认证令牌: {error}"}, status_code=401
            )
        except ValueError:
            return JSONResponse(
                {"success": False, "message": "令牌缺少必需的用户信息"}, status_code=401
            )
        request.session["authenticated"] = True
        request.session["user_info"] = user_info
        return JSONResponse({"success": True, "message": "认证成功", "user_info": user_info})

    @app.get("/api/auth/logout")
    @app.post("/api/auth/logout")
    async def auth_logout(request: Request) -> dict[str, object]:
        request.session.clear()
        return {"success": True, "message": "已退出登录", "azure_logout_url": azure_logout_url}
