from datetime import timedelta
import email.utils
import html
import os
import re
import time
import urllib.request
import urllib.parse
import json
import datetime as dt
import xml.etree.ElementTree as ET
from django.utils import timezone
from django.db import connection
from django.db.models import Sum
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import StrikeRollupMinute, LightningStrike, CountryStrike


NEWS_CACHE_SECONDS = 60 * 30
NEWS_DEFAULT_LIMIT = 5
NEWS_MAX_LIMIT = 8
NEWS_SAFE_RE = re.compile(r"[^a-zA-Z0-9À-ÿ\s'’._-]")


def _direct_news_url(link):
    parsed = urllib.parse.urlparse(link)
    host = parsed.netloc.lower()
    if host.endswith("bing.com"):
        nested = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
        if nested:
            return nested
    if host.endswith("google.com") or host.endswith("googleusercontent.com"):
        return ""
    return link


def _source_from_url(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


@api_view(["GET"])
def recent_strikes(request):
    """
    ?minutes=60 ?older_than=0 ?limit=5000
    ?after=<iso>     only strikes received strictly after this (delta polling)
    ?downsample=N    return every Nth row (old bands don't need full density)
    """
    minutes = int(request.GET.get("minutes", 10))
    older_than = int(request.GET.get("older_than", 0))
    limit = min(int(request.GET.get("limit", 5000)), 200000)  # raised cap
    downsample = max(1, int(request.GET.get("downsample", 1)))
    after = request.GET.get("after")
    now = timezone.now()
    since = now - timedelta(minutes=minutes)

    qs = LightningStrike.objects.filter(received_at__gte=since)
    if older_than > 0:
        qs = qs.filter(received_at__lt=now - timedelta(minutes=older_than))
    if after:
        try:
            qs = qs.filter(received_at__gt=dt.datetime.fromisoformat(after))
        except ValueError:
            pass
    qs = (qs.order_by("-received_at")
            .values("lat", "lon", "quality", "timestamp", "received_at")[:limit])

    rows = list(qs)
    if downsample > 1:
        rows = rows[::downsample]

    return Response({
        "minutes": minutes,
        "older_than": older_than,
        "count": len(rows),
        "strikes": rows,
    })


@api_view(["GET"])
def strikes_per_minute(request):
    minutes = int(request.GET.get("minutes", 15))
    now = timezone.now()
    current_min = now.replace(second=0, microsecond=0)
    since = current_min - timedelta(minutes=minutes - 1)

    rows = (StrikeRollupMinute.objects
            .filter(bucket__gte=since)
            .values("bucket")
            .annotate(n=Sum("count")))

    def k(d):
        if timezone.is_naive(d):
            d = timezone.make_aware(d, dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")

    by_bucket = {k(r["bucket"]): r["n"] for r in rows}

    series = []
    for i in range(minutes):
        b = since + timedelta(minutes=i)
        series.append({"minute": b.isoformat(), "count": by_bucket.get(k(b), 0)})

    return Response({"minutes": minutes, "series": series})


@api_view(["GET"])
def country_strikes(request):
    limit = min(int(request.GET.get("limit", 5000)), 5000)
    country = request.GET.get("country")

    if country:
        cc = country.upper()
        since = timezone.now() - timedelta(hours=1)
        last_hour = CountryStrike.objects.filter(country=cc, received_at__gte=since).count()
        rows = (CountryStrike.objects
                .filter(country=cc)
                .order_by("-received_at")
                .values("lat", "lon", "timestamp", "quality", "received_at")[:limit])
        return Response({
            cc: list(rows),
            "_meta": {
                "country": cc,
                "limit": limit,
                "lastHour": last_hour,
                "cappedLastHour": last_hour > limit,
            },
        })

    sql = """
        SELECT country, lat, lon, timestamp, quality, received_at FROM (
            SELECT country, lat, lon, timestamp, quality, received_at,
                   ROW_NUMBER() OVER (PARTITION BY country ORDER BY received_at DESC) AS rn
            FROM lightning_countrystrike
        ) t
        WHERE rn <= %s
        ORDER BY country, received_at DESC
    """
    out = {}
    with connection.cursor() as cur:
        cur.execute(sql, [limit])
        for cc, lat, lon, ts, quality, recv in cur.fetchall():
            out.setdefault(cc, []).append(
                {"lat": lat, "lon": lon, "timestamp": ts,
                 "quality": quality, "received_at": recv}
            )
    return Response(out)


@api_view(["GET"])
def country_news(request):
    """
    Small, cached local-language news feed for SEO country pages.

    ?country=FR&lang=fr&q=foudre%20France&limit=5
    """
    country = (request.GET.get("country") or "").strip().upper()
    lang = (request.GET.get("lang") or "en").strip().lower()
    query = (request.GET.get("q") or "lightning").strip()
    try:
        limit = min(max(int(request.GET.get("limit", NEWS_DEFAULT_LIMIT)), 1), NEWS_MAX_LIMIT)
    except ValueError:
        limit = NEWS_DEFAULT_LIMIT

    if not re.fullmatch(r"[A-Z]{2}", country):
        return Response({"error": "country_required"}, status=400)
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", lang):
        return Response({"error": "bad_lang"}, status=400)

    query = NEWS_SAFE_RE.sub(" ", query)[:120].strip() or "lightning"
    ckey = f"news:v2:{country}:{lang}:{query}:{limit}"
    cached = cache.get(ckey)
    if cached:
        return Response(cached)

    params = urllib.parse.urlencode({
        "q": query,
        "format": "rss",
        "cc": country.lower(),
        "setlang": lang,
    })
    url = f"https://www.bing.com/news/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "LightningMapGame/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            data = r.read()
        root = ET.fromstring(data)
    except Exception:
        return Response({"error": "news_fetch_failed"}, status=502)

    articles = []
    for item in root.findall("./channel/item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = _direct_news_url((item.findtext("link") or "").strip())
        source = html.unescape((item.findtext("source") or "").strip()) or _source_from_url(link)
        published_raw = (item.findtext("pubDate") or "").strip()
        published = ""
        if published_raw:
            try:
                published = email.utils.parsedate_to_datetime(published_raw).isoformat()
            except (TypeError, ValueError):
                published = published_raw
        if title and link:
            articles.append({
                "title": title,
                "url": link,
                "source": source,
                "publishedAt": published,
            })
        if len(articles) >= limit:
            break

    out = {
        "country": country,
        "lang": lang,
        "query": query,
        "articles": articles,
        "fetchedAt": timezone.now().isoformat(),
    }
    cache.set(ckey, out, NEWS_CACHE_SECONDS)
    return Response(out)


OWM_KEY = os.environ.get("OWM_API_KEY")  # server-only, NO NEXT_PUBLIC_
OWM_RATE_LIMIT_PER_MINUTE = int(os.environ.get("OWM_RATE_LIMIT_PER_MINUTE", "59"))
OWM_NOW_CACHE_SECONDS = int(os.environ.get("OWM_NOW_CACHE_SECONDS", "600"))
OWM_TILE_CACHE_SECONDS = int(os.environ.get("OWM_TILE_CACHE_SECONDS", "1800"))

# Allowlist so the proxy can't be abused to fetch arbitrary OWM layers/paths.
OWM_TILE_LAYERS = {"clouds_new", "precipitation_new", "temp_new", "wind_new"}


def _owm_rate_limited_response():
    return JsonResponse(
        {
            "error": "owm_rate_limited",
            "detail": "OpenWeatherMap free-plan limit protected; retry shortly.",
        },
        status=429,
        headers={"Retry-After": "60"},
    )


def _allow_owm_miss():
    """
    Count only cache misses that would hit OpenWeatherMap. With Redis cache this
    is shared across Cloud Run requests/users and keeps the key below 60 rpm.
    """
    if OWM_RATE_LIMIT_PER_MINUTE <= 0:
        return True

    minute = int(time.time() // 60)
    key = f"owm:rpm:{minute}"
    added = cache.add(key, 1, 120)
    if added:
        return True

    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 120)
        return True
    return count <= OWM_RATE_LIMIT_PER_MINUTE


@api_view(["GET"])
def weather_now(request):
    if not OWM_KEY:
        return Response({"error": "owm_key_unset"}, status=503)
    try:
        lat = float(request.GET["lat"])
        lon = float(request.GET["lon"])
    except (KeyError, ValueError):
        return Response({"error": "lat_lon_required"}, status=400)

    ckey = f"owm:{round(lat, 2)}:{round(lon, 2)}"
    cached = cache.get(ckey)
    if cached:
        return Response(cached)

    if not _allow_owm_miss():
        return _owm_rate_limited_response()

    q = urllib.parse.urlencode({"lat": lat, "lon": lon, "units": "metric", "appid": OWM_KEY})
    url = f"https://api.openweathermap.org/data/2.5/weather?{q}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())
    except Exception:
        return Response({"error": "owm_fetch_failed"}, status=502)

    out = {
        "tempC": round(d.get("main", {}).get("temp", 0)),
        "clouds": round(d.get("clouds", {}).get("all", 0)),
        "windKph": round(d.get("wind", {}).get("speed", 0) * 3.6),
        "humidity": round(d.get("main", {}).get("humidity", 0)),
        "main": (d.get("weather") or [{}])[0].get("main", "—"),
        "icon": (d.get("weather") or [{}])[0].get("icon", ""),
        "country": d.get("sys", {}).get("country"),
    }
    cache.set(ckey, out, OWM_NOW_CACHE_SECONDS)
    return Response(out)


def weather_tile(request, layer, z, x, y):
    """
    Server-side proxy for OpenWeatherMap raster tiles. The OWM key stays on the
    server (env OWM_API_KEY) and is never shipped to the browser. Tiles are
    cached server-side (10 min) and marked cacheable for the browser/CDN.

    GET /api/weather/tiles/<layer>/<z>/<x>/<y>.png
    """
    if not OWM_KEY:
        return JsonResponse({"error": "owm_key_unset"}, status=503)
    if layer not in OWM_TILE_LAYERS:
        return JsonResponse({"error": "bad_layer"}, status=400)

    ckey = f"owmtile:{layer}:{z}:{x}:{y}"
    data = cache.get(ckey)
    if data is None:
        if not _allow_owm_miss():
            return _owm_rate_limited_response()

        url = f"https://tile.openweathermap.org/map/{layer}/{z}/{x}/{y}.png?appid={OWM_KEY}"
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                data = r.read()
        except Exception:
            return JsonResponse({"error": "owm_tile_failed"}, status=502)
        cache.set(ckey, data, OWM_TILE_CACHE_SECONDS)

    resp = HttpResponse(data, content_type="image/png")
    resp["Cache-Control"] = f"public, max-age={OWM_TILE_CACHE_SECONDS}"
    return resp


@api_view(["GET"])
def strikes_count(request):
    """
    Strike count inside a lat/lon bounding box over the last `minutes`.
    Consumed by components/map/LightningMap2D.tsx.
    ?min_lat= &max_lat= &min_lon= &max_lon= &minutes=60
    """
    try:
        min_lat = float(request.GET["min_lat"])
        max_lat = float(request.GET["max_lat"])
        min_lon = float(request.GET["min_lon"])
        max_lon = float(request.GET["max_lon"])
    except (KeyError, ValueError):
        return Response({"error": "bbox_required"}, status=400)

    minutes = int(request.GET.get("minutes", 60))
    since = timezone.now() - timedelta(minutes=minutes)
    count = LightningStrike.objects.filter(
        received_at__gte=since,
        lat__gte=min_lat, lat__lte=max_lat,
        lon__gte=min_lon, lon__lte=max_lon,
    ).count()
    return Response({"count": count})