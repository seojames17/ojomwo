import math
import os
from typing import Any, Literal, Optional

import requests


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def geocode_address(address: str) -> tuple[float, float]:
    address = (address or "").strip()
    if not address:
        raise ValueError("address is empty")

    headers = {"User-Agent": os.getenv("DINING_APP_USER_AGENT", "dining-voting-app/0.1")}
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1},
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("geocode failed: no results")
    lat = float(data[0]["lat"])
    lng = float(data[0]["lon"])
    return lat, lng


def _stub_places(lat: float, lng: float, limit: int) -> list[dict[str, Any]]:
    base_names = [
        "주변 맛집 후보 A",
        "주변 맛집 후보 B",
        "주변 맛집 후보 C",
        "주변 맛집 후보 D",
        "주변 맛집 후보 E",
        "주변 맛집 후보 F",
        "주변 맛집 후보 G",
        "주변 맛집 후보 H",
    ]
    base_offset_m = 350.0
    results: list[dict[str, Any]] = []
    for i in range(limit):
        angle = (i / max(1, limit)) * math.pi * 2
        dlat = (base_offset_m * math.cos(angle)) / 111320.0
        dlng = (base_offset_m * math.sin(angle)) / (111320.0 * math.cos(math.radians(lat)) + 1e-9)
        pl_lat = lat + dlat
        pl_lng = lng + dlng
        dist = haversine_m(lat, lng, pl_lat, pl_lng)
        results.append(
            {
                "name": base_names[i % len(base_names)],
                "address": "데모 주소(실제 API 결과 아님)",
                "lat": pl_lat,
                "lng": pl_lng,
                "distance_m": dist,
                "rating": None,
                "price_level": None,
                "external_id": f"stub_{i}",
            }
        )
    return results


def _search_google_places(
    *,
    lat: float,
    lng: float,
    radius_m: int,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_PLACES_API_KEY is not set")

    params = {
        "key": api_key,
        "location": f"{lat},{lng}",
        "radius": max(1, int(radius_m)),
        "keyword": query,
    }
    headers = {"User-Agent": os.getenv("DINING_APP_USER_AGENT", "dining-voting-app/0.1")}
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
        params=params,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") not in (None, "OK", "ZERO_RESULTS"):
        raise RuntimeError(f"google places failed: status={payload.get('status')}")

    results: list[dict[str, Any]] = []
    for r in payload.get("results", [])[: limit * 2]:
        loc = r.get("geometry", {}).get("location", {})
        pl_lat = float(loc.get("lat"))
        pl_lng = float(loc.get("lng"))
        dist = haversine_m(lat, lng, pl_lat, pl_lng)
        results.append(
            {
                "name": r.get("name") or "Unknown",
                "address": r.get("vicinity") or "",
                "lat": pl_lat,
                "lng": pl_lng,
                "distance_m": dist,
                "rating": r.get("rating"),
                "price_level": r.get("price_level"),
                "external_id": r.get("place_id"),
            }
        )
        if len(results) >= limit:
            break

    return results


def _search_kakao_places(
    *,
    lat: float,
    lng: float,
    radius_m: int,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    api_key = os.getenv("KAKAO_REST_API_KEY")
    if not api_key:
        raise ValueError("KAKAO_REST_API_KEY is not set")

    headers = {
        "Authorization": f"KakaoAK {api_key}",
        "User-Agent": os.getenv("DINING_APP_USER_AGENT", "dining-voting-app/0.1"),
    }

    results: list[dict[str, Any]] = []
    page = 1
    max_page = 45
    while len(results) < limit and page <= max_page:
        remaining = limit - len(results)
        size = min(15, max(1, remaining))
        params = {
            "query": query,
            "x": str(lng),
            "y": str(lat),
            "radius": str(max(1, int(radius_m))),
            "size": str(size),
            "page": str(page),
        }

        resp = requests.get(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers=headers,
            params=params,
            timeout=30,
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            body_preview = (resp.text or "").strip()
            if len(body_preview) > 500:
                body_preview = body_preview[:500] + "...(truncated)"
            raise RuntimeError(
                f"Kakao Local API failed (status={resp.status_code}). Response: {body_preview}"
            ) from e

        payload = resp.json()
        docs = payload.get("documents") or []
        if not docs:
            break

        for d in docs:
            if len(results) >= limit:
                break
            x = d.get("x")
            y = d.get("y")
            if x is None or y is None:
                continue

            pl_lng = float(x)
            pl_lat = float(y)
            dist = d.get("distance")
            if dist is not None:
                try:
                    dist_m = float(dist)
                except Exception:
                    dist_m = haversine_m(lat, lng, pl_lat, pl_lng)
            else:
                dist_m = haversine_m(lat, lng, pl_lat, pl_lng)

            results.append(
                {
                    "name": str(d.get("place_name") or "Unknown"),
                    "address": str(d.get("road_address_name") or d.get("address_name") or ""),
                    "lat": pl_lat,
                    "lng": pl_lng,
                    "distance_m": dist_m,
                    "rating": None,
                    "price_level": None,
                    "external_id": str(d.get("id")) if d.get("id") is not None else d.get("place_url"),
                    "place_url": d.get("place_url"),
                }
            )

        meta = payload.get("meta") or {}
        if bool(meta.get("is_end")):
            break
        page += 1

    return results


def _search_naver_places(
    *,
    lat: float,
    lng: float,
    radius_m: int,
    query: str,
    limit: int,
    location_text: Optional[str],
) -> list[dict[str, Any]]:
    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET is not set")

    center_query = query.strip()
    if location_text and location_text.strip():
        center_query = f"{center_query} {location_text.strip()}"

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    display = 5
    results: list[dict[str, Any]] = []

    for page in range(3):
        start = 1 + page * display
        resp = requests.get(
            "https://openapi.naver.com/v1/search/local.json",
            headers=headers,
            params={"query": center_query, "display": str(display), "start": str(start), "sort": "random"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items") or []

        for it in items:
            try:
                mapx = float(it.get("mapx"))
                mapy = float(it.get("mapy"))
            except Exception:
                continue

            dist_m = haversine_m(lat, lng, mapy, mapx)
            if dist_m > float(radius_m):
                continue

            name = it.get("title") or "Unknown"
            address = it.get("roadAddress") or it.get("address") or ""

            results.append(
                {
                    "name": str(name),
                    "address": str(address),
                    "lat": mapy,
                    "lng": mapx,
                    "distance_m": dist_m,
                    "rating": None,
                    "price_level": None,
                    "external_id": it.get("link"),
                }
            )

            if len(results) >= limit:
                return results[:limit]

    return results if results else _stub_places(lat, lng, limit)


def _resolve_provider(provider_preference: str) -> Literal["kakao", "naver", "google", "stub"]:
    if provider_preference == "kakao":
        return "kakao"
    if provider_preference == "naver":
        return "naver"
    if provider_preference == "google":
        return "google"
    if provider_preference == "auto":
        if os.getenv("KAKAO_REST_API_KEY"):
            return "kakao"
        if os.getenv("NAVER_CLIENT_ID") and os.getenv("NAVER_CLIENT_SECRET"):
            return "naver"
        if os.getenv("GOOGLE_PLACES_API_KEY"):
            return "google"
        return "stub"
    return "stub"


def search_nearby_places(
    *,
    lat: float,
    lng: float,
    radius_m: int,
    query: str,
    limit: int,
    provider_preference: str = "auto",
    location_text: Optional[str] = None,
) -> list[dict[str, Any]]:
    provider = _resolve_provider(provider_preference)

    if provider == "stub":
        return _stub_places(lat, lng, limit)
    if provider == "kakao":
        return _search_kakao_places(lat=lat, lng=lng, radius_m=radius_m, query=query, limit=limit)
    if provider == "google":
        return _search_google_places(lat=lat, lng=lng, radius_m=radius_m, query=query, limit=limit)
    if provider == "naver":
        return _search_naver_places(
            lat=lat,
            lng=lng,
            radius_m=radius_m,
            query=query,
            limit=limit,
            location_text=location_text,
        )
    return _stub_places(lat, lng, limit)
