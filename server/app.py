"""mikeos-enterprises-web — enterprises.osmike.com

Public human-facing UI over the MikeOS company registries (Sweden + France).
Serves `public/` and proxies the two bearer-gated backend APIs server-side, so
the OSM_TOKEN never reaches the browser.

Backends (reached by container name over the shared `deploy_default` network):
  http://sweden-enterprises-api:8000   /health /search /company /near
  http://france-enterprises-api:8000   /health /near /lookup [/search if deployed]

No database. One secret: OSM_TOKEN (env). Small in-memory TTL cache keeps the
page fast and shields the backends from repeat traffic.
"""
import asyncio
import os
import re
import time

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

OSM_TOKEN = os.environ.get("OSM_TOKEN", "")
SE_BASE = os.environ.get("SE_BASE", "http://sweden-enterprises-api:8000")
FR_BASE = os.environ.get("FR_BASE", "http://france-enterprises-api:8000")
TIMEOUT = httpx.Timeout(6.0, connect=3.0)
CACHE_TTL = 60.0          # seconds — stats + repeated identical queries
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


def _cache_get(key: str):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
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


@app.get("/api/{rest:path}")
async def api_404(rest: str):
    return JSONResponse({"ok": False, "detail": "unknown endpoint"}, status_code=404)


app.mount("/", StaticFiles(directory=PUBLIC, html=True), name="static")
