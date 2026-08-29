import os
import time
from datetime import datetime, timezone

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------
# Configuration (toutes les valeurs viennent des variables d'environnement
# Railway — aucun secret n'est écrit dans ce fichier)
# --------------------------------------------------------------------------
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", "")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")  # protège l'accès à ton MCP

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
# Cache du token en mémoire. Le refresh_token "de départ" vient de la
# variable d'environnement TIKTOK_REFRESH_TOKEN ; une fois le serveur lancé,
# tout est rafraîchi automatiquement en mémoire (pas de fichier, pas de DB).
# --------------------------------------------------------------------------
_token_cache = {
    "access_token": None,
    "expires_at": 0,
    "refresh_token": os.environ.get("TIKTOK_REFRESH_TOKEN", ""),
}


async def get_valid_access_token() -> str:
    """Retourne un access_token valide, en le rafraîchissant si besoin."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    refresh_token = _token_cache["refresh_token"]
    if not refresh_token:
        raise RuntimeError(
            "Aucun refresh token TikTok configuré. "
            "Va sur l'URL /auth de ton serveur pour connecter ton compte."
        )

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
            url, headers={"Authorization": f"Bearer {token}"}, params=params or {}
        )
    return resp.json()


async def tiktok_post(url: str, json_body: dict | None = None, params: dict | None = None) -> dict:
    token = await get_valid_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params=params or {},
            json=json_body or {},
        )
    return resp.json()


# --------------------------------------------------------------------------
# Serveur MCP et outils exposés à Claude
# --------------------------------------------------------------------------
mcp = FastMCP("tiktok-mcp")


@mcp.tool()
async def get_profile() -> dict:
    """Récupère les infos du profil TikTok connecté (nom, followers, nombre de vidéos, etc.)."""
    data = await tiktok_get(TIKTOK_USER_INFO_URL, params={"fields": USER_FIELDS})
    return data.get("data", {}).get("user", data)


@mcp.tool()
async def get_recent_videos(max_count: int = 20) -> dict:
    """Récupère les vidéos publiées les plus récentes (max_count, 20 max par appel TikTok)."""
    data = await tiktok_post(
        TIKTOK_VIDEO_LIST_URL,
        params={"fields": VIDEO_FIELDS},
        json_body={"max_count": max_count},
    )
    videos = data.get("data", {}).get("videos", [])
    return {"count": len(videos), "videos": videos}


@mcp.tool()
async def get_today_videos() -> dict:
    """Récupère uniquement les vidéos publiées aujourd'hui (UTC)."""
    result = await get_recent_videos(max_count=20)
    videos = result.get("videos", [])
    today = datetime.now(timezone.utc).date()
    today_videos = [
        v
        for v in videos
        if datetime.fromtimestamp(v.get("create_time", 0), tz=timezone.utc).date() == today
    ]
    return {"count": len(today_videos), "videos": today_videos}


@mcp.tool()
async def get_video_stats(video_id: str) -> dict:
    """Récupère les statistiques détaillées d'une vidéo précise à partir de son ID."""
    data = await tiktok_post(
        TIKTOK_VIDEO_LIST_URL,
        params={"fields": VIDEO_FIELDS},
        json_body={"max_count": 20},
    )
    videos = data.get("data", {}).get("videos", [])
    for v in videos:
        if str(v.get("id")) == str(video_id):
            return v
    return {"error": f"Vidéo {video_id} introuvable dans les 20 dernières vidéos."}


# --------------------------------------------------------------------------
# Sécurité : un jeton Bearer simple protège l'endpoint MCP (/mcp).
# Les routes /auth et /callback restent ouvertes pour le setup TikTok.
# --------------------------------------------------------------------------
class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/auth") or request.url.path.startswith("/callback"):
            return await call_next(request)
        if MCP_AUTH_TOKEN:
            auth_header = request.headers.get("authorization", "")
            if auth_header != f"Bearer {MCP_AUTH_TOKEN}":
                return PlainTextResponse("Non autorisé", status_code=401)
        return await call_next(request)


# --------------------------------------------------------------------------
# Routes OAuth TikTok — à utiliser UNE SEULE FOIS pour connecter ton compte.
# --------------------------------------------------------------------------
async def auth_start(request: Request):
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "response_type": "code",
        "scope": "user.info.basic,video.list",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": "setup",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(f"{TIKTOK_AUTH_URL}?{query}")


async def auth_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<p>Erreur : pas de code reçu de TikTok.</p>", status_code=400)

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
    refresh_token = data.get("refresh_token")
    access_token = data.get("access_token")

    if not refresh_token:
        return HTMLResponse(f"<pre>Erreur TikTok : {data}</pre>", status_code=400)

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600))
    _token_cache["refresh_token"] = refresh_token

    return HTMLResponse(
        f"""
        <h2>Connexion TikTok réussie ✅</h2>
        <p>Copie cette valeur et colle-la dans la variable Railway
        <b>TIKTOK_REFRESH_TOKEN</b>, puis redéploie le service :</p>
        <textarea rows="4" cols="80">{refresh_token}</textarea>
        <p>Tu peux ensuite fermer cette page.</p>
        """
    )


# --------------------------------------------------------------------------
# Application ASGI finale
# --------------------------------------------------------------------------
routes = [
    Route("/auth", auth_start, methods=["GET"]),
    Route("/callback", auth_callback, methods=["GET"]),
    Mount("/", app=mcp.streamable_http_app()),
]

app = Starlette(routes=routes, middleware=[Middleware(BearerAuthMiddleware)])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
