"""mikeos-enterprises-web — enterprises.osmike.com

Public human-facing UI over the MikeOS company registries (Sweden + France).
Serves `public/` and proxies the two bearer-gated backend APIs server-side, so
the OSM_TOKEN never reaches the browser.

Backends (reached by container name over the shared `deploy_default` network):
  http://sweden-enterprises-api:8000   /health /search /company /near
  http://france-enterprises-api:8000   /health /near /lookup /search /company
  http://mikeos-sweden-scb:3000        POST /api/v1/companies/by-orgnr  (X-API-Key)

Also server-side renders shareable profile pages /se/{orgnr} and /fr/{siret}
with real <title>/OG tags. No database. Secrets: OSM_TOKEN + SCB_KEY (env).
Small in-memory TTL caches keep the page fast and shield the backends.
"""
import asyncio
import html
import json
import os
import re
import time

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

OSM_TOKEN = os.environ.get("OSM_TOKEN", "")
SCB_KEY = os.environ.get("SCB_KEY", "")
SE_BASE = os.environ.get("SE_BASE", "http://sweden-enterprises-api:8000")
FR_BASE = os.environ.get("FR_BASE", "http://france-enterprises-api:8000")
SCB_BASE = os.environ.get("SCB_BASE", "http://mikeos-sweden-scb:3000")
TIMEOUT = httpx.Timeout(6.0, connect=3.0)
SCB_TIMEOUT = httpx.Timeout(10.0, connect=3.0)  # SCB fronts a rate-limited, queued upstream
CACHE_TTL = 60.0          # seconds — stats + repeated identical queries
SCB_TTL = 900.0           # 15 min per orgnr — spare the 10 req/10 s SCB upstream
CACHE_MAX = 512           # entries; simple bound so memory can't creep

app = FastAPI(title="mikeos-enterprises-web", docs_url=None, redoc_url=None)
PUBLIC = os.path.join(os.path.dirname(__file__), "..", "public")

_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, object]] = {}


@app.on_event("startup")
async def _startup():
    global _client
    _client = httpx.AsyncClient(timeout=TIMEOUT,
                                headers={"Authorization": f"Bearer {OSM_TOKEN}"})


@app.on_event("shutdown")
async def _shutdown():
    if _client:
        await _client.aclose()


def _cache_get(key: str, ttl: float = CACHE_TTL):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(key: str, value):
    if len(_cache) >= CACHE_MAX:
        # drop the oldest half — cheap, good enough for a 60 s TTL cache
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[: CACHE_MAX // 2]:
            _cache.pop(k, None)
    _cache[key] = (time.monotonic(), value)


async def _get(base: str, path: str, params: dict | None = None):
    """Proxy one backend GET. Cached; backend errors become clean HTTP errors."""
    key = f"{base}{path}?{sorted((params or {}).items())}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        r = await _client.get(f"{base}{path}", params=params)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="registry backend timed out")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="registry backend unreachable")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="not found")
    if r.status_code >= 400:
        detail = "registry backend error"
        try:
            detail = r.json().get("detail", detail)
        except Exception:
            pass
        raise HTTPException(status_code=502 if r.status_code >= 500 else 400, detail=detail)
    data = r.json()
    _cache_put(key, data)
    return data


def _check_bbox(bbox: str) -> str:
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except Exception:
        raise HTTPException(status_code=400, detail="bbox must be 'west,south,east,north'")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(status_code=400, detail="bad bbox")
    if (e - w) * (n - s) > 0.25:
        raise HTTPException(status_code=400, detail="bbox too large (max 0.25 deg²)")
    return bbox


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "mikeos-enterprises-web"}


@app.get("/api/stats")
async def stats():
    """Merged /health of both registries. Each side degrades independently."""
    async def side(name, base):
        try:
            return name, await _get(base, "/health")
        except HTTPException:
            return name, None
    results = dict(await asyncio.gather(side("se", SE_BASE), side("fr", FR_BASE)))
    se, fr = results.get("se"), results.get("fr")
    out = {
        "ok": bool(se or fr),
        "se": {"ok": bool(se), **({k: se[k] for k in ("companies", "establishments", "geocoded") if se and k in se} if se else {})},
        "fr": {"ok": bool(fr), **({"establishments": fr["establishments"]} if fr and "establishments" in fr else {})},
    }
    out["total"] = (out["se"].get("companies", 0) or 0) + (out["fr"].get("establishments", 0) or 0)
    return out


# ---------- Sweden ----------

@app.get("/api/se/search")
async def se_search(q: str = Query(..., min_length=2, max_length=100),
                    limit: int = Query(20, ge=1, le=100)):
    return await _get(SE_BASE, "/search", {"q": q, "limit": limit})


@app.get("/api/se/company")
async def se_company(orgnr: str = Query(..., min_length=10, max_length=13)):
    if not re.fullmatch(r"[\d\-]{10,13}", orgnr):
        raise HTTPException(status_code=400, detail="bad orgnr")
    return await _get(SE_BASE, "/company", {"orgnr": orgnr})


@app.get("/api/se/near")
async def se_near(bbox: str = Query(...), limit: int = Query(200, ge=1, le=400)):
    return await _get(SE_BASE, "/near", {"bbox": _check_bbox(bbox), "limit": limit})


# ---------- France ----------

@app.get("/api/fr/near")
async def fr_near(bbox: str = Query(...), limit: int = Query(200, ge=1, le=400)):
    return await _get(FR_BASE, "/near", {"bbox": _check_bbox(bbox), "limit": limit})


@app.get("/api/fr/search")
async def fr_search(q: str = Query(..., min_length=2, max_length=100),
                    limit: int = Query(20, ge=1, le=100)):
    return await _get(FR_BASE, "/search", {"q": q, "limit": limit})


@app.get("/api/fr/lookup")
async def fr_lookup(name: str = Query(..., min_length=1, max_length=200),
                    lat: float = Query(..., ge=-90, le=90),
                    lon: float = Query(..., ge=-180, le=180)):
    return await _get(FR_BASE, "/lookup", {"name": name, "lat": lat, "lon": lon})


@app.get("/api/fr/company")
async def fr_company(siret: str = Query(..., min_length=14, max_length=17)):
    d = _digits(siret)
    if len(d) != 14:
        raise HTTPException(status_code=400, detail="siret must be 14 digits")
    return await _get(FR_BASE, "/company", {"siret": d})


# ---------- SCB live registry (Sweden, profile pages only) ----------

@app.get("/api/se/scb")
async def se_scb(orgnr: str = Query(..., min_length=10, max_length=13)):
    """Live Allmänna företagsregistret row via the internal mikeos-sweden-scb service.
    Heavily cached (15 min/orgnr) and only ever called from a profile page — the SCB
    upstream allows 10 req/10 s for the whole ecosystem."""
    o = _digits(orgnr)
    if len(o) == 12:
        o = o[-10:]
    if len(o) != 10:
        raise HTTPException(status_code=400, detail="orgnr must be 10 digits")
    if not SCB_KEY:
        raise HTTPException(status_code=503, detail="live registry not configured")
    key = f"scb:{o}"
    cached = _cache_get(key, ttl=SCB_TTL)
    if cached is not None:
        return cached
    try:
        r = await _client.post(
            f"{SCB_BASE}/api/v1/companies/by-orgnr",
            json={"orgNr": [o]},
            headers={"X-API-Key": SCB_KEY, "Authorization": ""},
            timeout=SCB_TIMEOUT,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="live registry timed out")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="live registry unreachable")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail="live registry error")
    body = r.json()
    rows = body.get("data") or []
    out = {"ok": bool(rows), "company": rows[0] if rows else None,
           "notFound": o in (body.get("notFound") or [])}
    _cache_put(key, out)
    return out


@app.get("/api/{rest:path}")
async def api_404(rest: str):
    return JSONResponse({"ok": False, "detail": "unknown endpoint"}, status_code=404)


# ---------- shareable profile pages (server-side rendered shell) ----------

def _digits(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def _template() -> str:
    with open(os.path.join(PUBLIC, "profile.html"), encoding="utf-8") as f:
        return f.read()


def _render(head: str, boot: dict) -> str:
    # </ must not terminate the boot <script>; escape it inside JSON strings.
    data = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return _template().replace("<!--HEAD-->", head).replace("__BOOT__", data)


def _head(title: str, desc: str, url: str) -> str:
    t, d, u = html.escape(title), html.escape(desc), html.escape(url)
    return (f"<title>{t}</title>\n"
            f'<meta name="description" content="{d}" />\n'
            f'<meta property="og:title" content="{t}" />\n'
            f'<meta property="og:description" content="{d}" />\n'
            f'<meta property="og:type" content="profile" />\n'
            f'<meta property="og:url" content="{u}" />\n'
            f'<meta name="twitter:card" content="summary" />\n'
            f'<link rel="canonical" href="{u}" />')


def _shorten(s: str | None, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _404_page(country: str) -> HTMLResponse:
    head = _head("Company not found — MikeOS Enterprises",
                 "No company with that number in the registry.",
                 "https://enterprises.osmike.com/")
    return HTMLResponse(_render(head, {"notFound": True, "country": country}),
                        status_code=404)


@app.get("/se/{orgnr}", response_class=HTMLResponse)
async def se_profile(orgnr: str):
    o = _digits(orgnr)
    if len(o) == 12:
        o = o[-10:]
    if len(o) != 10:
        return _404_page("se")
    try:
        c = await _get(SE_BASE, "/company", {"orgnr": o})
    except HTTPException as e:
        if e.status_code == 404:
            return _404_page("se")
        raise
    name = c.get("name") or "Company"
    desc = _shorten(c.get("business_desc")) or \
        f"Swedish company {o[:6]}-{o[6:]} — full registry profile on MikeOS Enterprises."
    head = _head(f"{name} — MikeOS Enterprises", desc,
                 f"https://enterprises.osmike.com/se/{o}")
    return HTMLResponse(_render(head, {"country": "se", "company": c}))


@app.get("/fr/{siret}", response_class=HTMLResponse)
async def fr_profile(siret: str):
    d = _digits(siret)
    if len(d) != 14:
        return _404_page("fr")
    try:
        c = await _get(FR_BASE, "/company", {"siret": d})
    except HTTPException as e:
        if e.status_code == 404:
            return _404_page("fr")
        raise
    name = c.get("name") or "Établissement"
    bits = [b for b in [c.get("kind"), "SIRENE registry storefront, France"] if b]
    head = _head(f"{name} — MikeOS Enterprises", _shorten(" · ".join(bits)),
                 f"https://enterprises.osmike.com/fr/{d}")
    return HTMLResponse(_render(head, {"country": "fr", "company": c}))


app.mount("/", StaticFiles(directory=PUBLIC, html=True), name="static")
