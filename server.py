import os
import contextlib
import secrets
import time
import base64
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, JSONResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_URL = "https://claude-tiktok-production.up.railway.app"

TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", f"{BASE_URL}/callback")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"

VIDEO_FIELDS = (
    "id,title,video_description,create_time,cover_image_url,"
    "share_url,view_count,like_count,comment_count,share_count"
)
USER_FIELDS = (
    "open_id,display_name,avatar_url,follower_count,"
    "following_count,likes_count,video_count"
)

# --------------------------------------------------------------------------
# TikTok token cache
# --------------------------------------------------------------------------
_token_cache = {
    "access_token": None,
    "expires_at": 0,
    "refresh_token": os.environ.get("TIKTOK_REFRESH_TOKEN", ""),
}

async def get_valid_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    refresh_token = _token_cache["refresh_token"]
    if not refresh_token:
        raise RuntimeError("Aucun refresh token TikTok configuré.")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TIKTOK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": TIKTOK_CLIENT_KEY,
                "client_secret": TIKTOK_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )

    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Impossible de rafraîchir le token TikTok : {data}")

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    if data.get("refresh_token"):
        _token_cache["refresh_token"] = data["refresh_token"]

    return _token_cache["access_token"]

async def tiktok_get(url: str, params: dict | None = None) -> dict:
    token = await get_valid_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
    return resp.json()

async def tiktok_post(url: str, json_body: dict | None = None, params: dict | None = None) -> dict:
    token = await get_valid_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params=params or {},
            json=json_body or {},
        )
    return resp.json()

# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
mcp = FastMCP("tiktok-mcp")

@mcp.tool()
async def get_profile() -> dict:
    """Récupère les infos du profil TikTok connecté."""
    data = await tiktok_get(TIKTOK_USER_INFO_URL, params={"fields": USER_FIELDS})
    return data.get("data", {}).get("user", data)

@mcp.tool()
async def get_recent_videos(max_count: int = 20) -> dict:
    """Récupère les vidéos publiées les plus récentes."""
    max_count = max(1, min(max_count, 20))
    data = await tiktok_post(
        TIKTOK_VIDEO_LIST_URL,
        params={"fields": VIDEO_FIELDS},
        json_body={"max_count": max_count},
    )
    videos = data.get("data", {}).get("videos", [])
    return {"count": len(videos), "videos": videos}

@mcp.tool()
async def get_today_videos() -> dict:
    """Récupère les vidéos publiées aujourd'hui (UTC)."""
    result = await get_recent_videos(max_count=20)
    today = datetime.now(timezone.utc).date()
    today_videos = [
        v for v in result.get("videos", [])
        if datetime.fromtimestamp(v.get("create_time", 0), tz=timezone.utc).date() == today
    ]
    return {"count": len(today_videos), "videos": today_videos}

@mcp.tool()
async def get_video_stats(video_id: str) -> dict:
    """Récupère les statistiques d'une vidéo précise."""
    result = await get_recent_videos(max_count=20)
    for video in result.get("videos", []):
        if str(video.get("id")) == str(video_id):
            return video
    return {"error": f"Vidéo {video_id} introuvable dans les 20 dernières vidéos."}

# --------------------------------------------------------------------------
# MCP OAuth 2.1 bridge
#
# Claude registers itself here, then /authorize sends the user through
# TikTok. After TikTok authentication, we issue a short-lived MCP token
# that Claude uses on /mcp.
# --------------------------------------------------------------------------
oauth_clients = {}
oauth_requests = {}
oauth_codes = {}
oauth_tokens = {}

def b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

async def oauth_metadata(request: Request):
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/authorize",
        "token_endpoint": f"{BASE_URL}/token",
        "registration_endpoint": f"{BASE_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["user.info.basic", "video.list"],
        "client_id_metadata_document_supported": False,
    })

async def protected_resource_metadata(request: Request):
    return JSONResponse({
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
        "scopes_supported": ["user.info.basic", "video.list"],
    })

async def oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    redirect_uris = body.get("redirect_uris") or []
    if not redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

    client_id = f"claude_{secrets.token_urlsafe(24)}"
    oauth_clients[client_id] = {
        "redirect_uris": redirect_uris,
        "client_name": body.get("client_name", "Claude"),
    }

    return JSONResponse({
        "client_id": client_id,
        "client_name": oauth_clients[client_id]["client_name"],
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)

async def oauth_authorize(request: Request):
    q = request.query_params
    client_id = q.get("client_id")
    redirect_uri = q.get("redirect_uri")
    response_type = q.get("response_type")
    scope = q.get("scope", "user.info.basic video.list")
    state = q.get("state", "")
    code_challenge = q.get("code_challenge", "")
    code_challenge_method = q.get("code_challenge_method", "")

    client = oauth_clients.get(client_id or "")
    if not client or redirect_uri not in client["redirect_uris"]:
        return PlainTextResponse("Client ou redirect_uri invalide.", status_code=400)
    if response_type != "code":
        return PlainTextResponse("response_type doit être code.", status_code=400)
    if not code_challenge or code_challenge_method != "S256":
        return PlainTextResponse("PKCE S256 est requis.", status_code=400)

    internal_state = secrets.token_urlsafe(32)
    oauth_requests[internal_state] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "scope": scope,
    }

    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "response_type": "code",
        "scope": "user.info.basic,video.list",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": internal_state,
    }
    return RedirectResponse(f"{TIKTOK_AUTH_URL}?{urlencode(params)}")

async def tiktok_callback(request: Request):
    code = request.query_params.get("code")
    internal_state = request.query_params.get("state")
    if not code or not internal_state:
        return HTMLResponse("<p>Erreur : réponse TikTok incomplète.</p>", status_code=400)

    oauth_request = oauth_requests.pop(internal_state, None)
    if not oauth_request:
        return HTMLResponse("<p>Session OAuth expirée ou invalide.</p>", status_code=400)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TIKTOK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": TIKTOK_CLIENT_KEY,
                "client_secret": TIKTOK_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": TIKTOK_REDIRECT_URI,
            },
        )

    data = resp.json()
    if "access_token" not in data:
        return HTMLResponse(f"<pre>Erreur TikTok : {data}</pre>", status_code=400)

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600))
    if data.get("refresh_token"):
        _token_cache["refresh_token"] = data["refresh_token"]

    local_code = secrets.token_urlsafe(32)
    oauth_codes[local_code] = {
        **oauth_request,
        "created_at": time.time(),
    }

    redirect_params = {"code": local_code}
    if oauth_request["state"]:
        redirect_params["state"] = oauth_request["state"]

    return RedirectResponse(
        oauth_request["redirect_uri"] + "?" + urlencode(redirect_params)
    )

async def oauth_token(request: Request):
    from urllib.parse import parse_qs
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw)
    form = {key: values[0] for key, values in parsed.items()}
    grant_type = form.get("grant_type")
    client_id = form.get("client_id")
    redirect_uri = form.get("redirect_uri")

    if grant_type == "authorization_code":
        code = form.get("code")
        verifier = form.get("code_verifier", "")
        record = oauth_codes.pop(str(code), None)

        if not record:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if time.time() - record["created_at"] > 300:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if b64url_sha256(verifier) != record["code_challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        oauth_tokens[access_token] = {
            "client_id": client_id,
            "expires_at": time.time() + 3600,
            "refresh_token": refresh_token,
        }

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh_token,
            "scope": record["scope"],
        })

    if grant_type == "refresh_token":
        incoming_refresh = form.get("refresh_token")
        for old_access, token_data in list(oauth_tokens.items()):
            if token_data["refresh_token"] == incoming_refresh:
                new_access = secrets.token_urlsafe(48)
                new_refresh = secrets.token_urlsafe(48)
                oauth_tokens.pop(old_access, None)
                oauth_tokens[new_access] = {
                    "client_id": token_data["client_id"],
                    "expires_at": time.time() + 3600,
                    "refresh_token": new_refresh,
                }
                return JSONResponse({
                    "access_token": new_access,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": new_refresh,
                    "scope": "user.info.basic video.list",
                })

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

# --------------------------------------------------------------------------
# Protect only the MCP endpoint with MCP OAuth tokens.
# --------------------------------------------------------------------------
class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        public_paths = (
            "/authorize",
            "/token",
            "/register",
            "/callback",
            "/auth",
            "/.well-known/",
        )
        if request.url.path.startswith(public_paths):
            return await call_next(request)

        # MCP endpoint: accept a token issued by this OAuth server.
        if request.url.path.startswith("/mcp") or request.method == "POST":
            auth = request.headers.get("authorization", "")
            token = auth.removeprefix("Bearer ").strip()

            token_data = oauth_tokens.get(token)
            if token_data and time.time() < token_data["expires_at"]:
                return await call_next(request)

            # Keep the old manual token as a fallback.
            if MCP_AUTH_TOKEN and token == MCP_AUTH_TOKEN:
                return await call_next(request)

            return PlainTextResponse(
                "Non autorisé",
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource"'
                    )
                },
            )

        return await call_next(request)

# --------------------------------------------------------------------------
# Legacy one-time TikTok login endpoint
# --------------------------------------------------------------------------
async def auth_start(request: Request):
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "response_type": "code",
        "scope": "user.info.basic,video.list",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": "setup",
    }
    return RedirectResponse(f"{TIKTOK_AUTH_URL}?{urlencode(params)}")

async def auth_callback(request: Request):
    # Kept for manual setup compatibility. Claude uses /authorize -> /callback.
    return await tiktok_callback(request)

# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
# Build the MCP sub-application once. Its session manager must be started
# by the parent application's lifespan when the MCP app is mounted.
mcp_app = mcp.streamable_http_app()

@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield

routes = [
    Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
    Route("/register", oauth_register, methods=["POST"]),
    Route("/authorize", oauth_authorize, methods=["GET"]),
    Route("/token", oauth_token, methods=["POST"]),
    Route("/auth", auth_start, methods=["GET"]),
    Route("/callback", tiktok_callback, methods=["GET"]),
    Mount("/", app=mcp_app),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(BearerAuthMiddleware)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
