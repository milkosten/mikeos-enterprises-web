# mikeos-enterprises-web

**Live:** https://enterprises.osmike.com

The public, human-facing UI over the MikeOS company registries — **Sweden**
(Bolagsverket/SCB: 904k+ companies with business descriptions and SNI codes) and
**France** (SIRENE/INSEE: 1.65M geocoded storefronts). Same design language as
[www.osmike.com](https://www.osmike.com) (Fraunces + Manrope, the "new dawn" palette).

## Shape

```
public/index.html     the whole UI — self-contained HTML/CSS/vanilla JS, no build step
server/app.py         FastAPI: serves public/ + proxies the registry APIs server-side
Dockerfile            python:3.12-slim + uvicorn on :8090
docker-compose.yml    container `mikeos-enterprises-web` on the shared deploy_default net
```

The browser only ever talks to `/api/*` on this app. The app holds the
`OSM_TOKEN` bearer secret (from `.env`) and forwards to the backends by container
name over `deploy_default`:

| Proxy route | Backend |
|---|---|
| `GET /api/stats` | merged `/health` of both registries |
| `GET /api/se/search?q=&limit=` | `sweden-enterprises-api:8000/search` (trigram) |
| `GET /api/se/company?orgnr=` | `…/company` — full row incl. verksamhetsbeskrivning |
| `GET /api/se/near?bbox=` | `…/near` |
| `GET /api/fr/search?q=&limit=` | `france-enterprises-api:8000/search` (trigram) |
| `GET /api/fr/near?bbox=&limit=` | `…/near` (bbox ≤ 0.25 deg²) |
| `GET /api/fr/company?siret=` | `…/company` — full establishments row |
| `GET /api/fr/lookup?name=&lat=&lon=` | `…/lookup` — crawled website/hours/phone |
| `GET /api/se/scb?orgnr=` | `mikeos-sweden-scb:3000` POST `/api/v1/companies/by-orgnr` (X-API-Key, 15 min cache, 10 s timeout) |

**Shareable profile pages** — `GET /se/{orgnr}` and `GET /fr/{siret}` server-side
render `public/profile.html` with a real `<title>`, meta description and OG tags
(shared links unfurl with the company name), and embed the registry row as boot
JSON. Unknown ids get a styled 404. The Swedish profile loads a "Live from SCB
(Statistics Sweden)" section async via `/api/se/scb`; the French profile loads
crawled website details async via `/api/fr/lookup`.

Responses are cached in-memory for 60 s (SCB: 15 min); backend calls time out at
6 s (SCB: 10 s) so a slow registry can never hang the page. Each country degrades
independently (error chips, not a blank page).

## Deploy (242)

```
rsync -a --exclude .git --exclude .env . root@91.98.177.242:/root/mikeos-enterprises-web/
ssh root@91.98.177.242 'cd /root/mikeos-enterprises-web && docker compose up -d --build'
```

`.env` on the server (mode 0600) provides `OSM_TOKEN` + `SCB_KEY`. Caddy
(`/opt/mikephotos/deploy/Caddyfile`) publishes it:

```
enterprises.osmike.com {
	encode zstd gzip
	reverse_proxy mikeos-enterprises-web:8090
}
```

DNS: Cloudflare A record `enterprises.osmike.com → 91.98.177.242` (DNS-only).

## Data & licences

- **Sweden** — Bolagsverket/SCB open registry bulk files; Swedish open government data.
- **France** — SIRENE (INSEE) stock + geolocation bulk files; Licence Ouverte 2.0.
