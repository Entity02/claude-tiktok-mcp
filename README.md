# claude-tiktok-mcp

MCP distant qui permet à Claude d'interroger ton compte TikTok à la demande
(pas de monitoring permanent — Claude n'appelle TikTok que quand tu poses une
question).

## Outils exposés à Claude

- `get_profile()` — infos du profil (followers, nb de vidéos, etc.)
- `get_recent_videos(max_count)` — dernières vidéos publiées
- `get_today_videos()` — vidéos publiées aujourd'hui
- `get_video_stats(video_id)` — stats détaillées d'une vidéo

## Variables d'environnement (à mettre dans Railway, jamais dans GitHub)

| Variable | Description |
|---|---|
| `TIKTOK_CLIENT_KEY` | Client Key de ton app TikTok for Developers |
| `TIKTOK_CLIENT_SECRET` | Client Secret de ton app TikTok for Developers |
| `TIKTOK_REDIRECT_URI` | URL publique Railway + `/callback`, ex : `https://ton-app.up.railway.app/callback` |
| `TIKTOK_REFRESH_TOKEN` | Obtenu une seule fois via `/auth` (voir plus bas), à coller après le premier login |
| `MCP_AUTH_TOKEN` | Un mot de passe que tu inventes, pour protéger ton MCP (utilisé par Claude pour se connecter) |

## Connexion initiale à TikTok (une seule fois)

1. Déploie le service une première fois sur Railway (sans `TIKTOK_REFRESH_TOKEN`, ce n'est pas grave).
2. Va dans TikTok for Developers → ton app → ajoute `TIKTOK_REDIRECT_URI` comme "Redirect URI" autorisée.
3. Ouvre `https://ton-app.up.railway.app/auth` dans ton navigateur, connecte-toi à TikTok.
4. La page `/callback` t'affiche un `refresh_token` : copie-le.
5. Colle-le dans la variable Railway `TIKTOK_REFRESH_TOKEN`, puis redéploie.

Après ça, le serveur se rafraîchit tout seul — tu n'as plus jamais besoin de
refaire cette étape (sauf si tu révoques l'accès côté TikTok).

## Connecter ce MCP à Claude

Dans Claude, ajoute un connecteur personnalisé avec :
- URL : `https://ton-app.up.railway.app/mcp`
- En-tête HTTP : `Authorization: Bearer <la valeur de MCP_AUTH_TOKEN>`

## Lancer en local (optionnel, pour tester)

```bash
pip install -r requirements.txt
export TIKTOK_CLIENT_KEY=...
export TIKTOK_CLIENT_SECRET=...
export TIKTOK_REDIRECT_URI=http://localhost:8000/callback
python server.py
```
