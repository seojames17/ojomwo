"""
오점뭐(오늘 점심 뭐먹지) - 선택장애 해소용 랜덤 추천(MVP)

- 입력: 현재 위치(IP 기반 대략), 검색 반경, 후보 풀 크기
- 동작: 주변 후보 N개 이상 조회 -> (거리 정보가 있으면) 가까운 후보 우선 범위를 잡고 -> 랜덤(가중 가능) 추천
- 데이터 소스(후보 검색):
  - dining_app/place_provider.py 를 재사용 (카카오 + 키 없으면 stub)
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import hashlib
import html
import math
import json
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd
import requests
import streamlit as st
import pydeck as pdk

try:
    from dining_app.place_provider import geocode_address, search_nearby_places
except Exception:
    # Streamlit Community Cloud 배포 시 패키지 경로 이슈가 있어도
    # 앱이 바로 죽지 않게 최소 fallback 구현을 제공합니다.
    def geocode_address(address: str) -> tuple[float, float]:
        address = (address or "").strip()
        if not address:
            raise ValueError("address is empty")
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "ojomwo-app/0.1"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            raise ValueError("geocode failed: no results")
        return float(data[0]["lat"]), float(data[0]["lon"])

    def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        r = 6371000.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lng2 - lng1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return r * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

    def search_nearby_places(
        *,
        lat: float,
        lng: float,
        radius_m: int,
        query: str,
        limit: int,
        provider_preference: str = "kakao",
        location_text: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        api_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
        if provider_preference != "kakao" or not api_key:
            raise RuntimeError("place provider unavailable: KAKAO_REST_API_KEY is required")

        headers = {
            "Authorization": f"KakaoAK {api_key}",
            "User-Agent": "ojomwo-app/0.1",
        }

        results: list[dict[str, Any]] = []
        page = 1
        while len(results) < int(limit) and page <= 45:
            remain = int(limit) - len(results)
            params = {
                "query": query or "맛집",
                "x": str(lng),
                "y": str(lat),
                "radius": str(max(1, int(radius_m))),
                "size": str(min(15, max(1, remain))),
                "page": str(page),
            }
            resp = requests.get(
                "https://dapi.kakao.com/v2/local/search/keyword.json",
                headers=headers,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
            docs = payload.get("documents") or []
            if not docs:
                break
            for d in docs:
                if len(results) >= int(limit):
                    break
                x = d.get("x")
                y = d.get("y")
                if x is None or y is None:
                    continue
                pl_lng = float(x)
                pl_lat = float(y)
                dist_raw = d.get("distance")
                try:
                    dist_m = float(dist_raw) if dist_raw is not None else _haversine_m(lat, lng, pl_lat, pl_lng)
                except Exception:
                    dist_m = _haversine_m(lat, lng, pl_lat, pl_lng)
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
            if bool((payload.get("meta") or {}).get("is_end")):
                break
            page += 1
        return results

try:
    from streamlit_geolocation import streamlit_geolocation
except Exception:
    streamlit_geolocation = None  # type: ignore[assignment]

try:
    from streamlit_js_eval import get_geolocation  # type: ignore[import-not-found]
except Exception:
    get_geolocation = None  # type: ignore[assignment]


def _unwrap_js_eval_location(loc: Any) -> Any:
    """streamlit_js_eval 이 value 래퍼나 JSON 문자열로 줄 때 정규화."""
    if loc is None:
        return None
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except Exception:
            return None
    if isinstance(loc, dict) and isinstance(loc.get("value"), dict):
        inner = loc["value"]
        if "coords" in inner or "error" in inner or "latitude" in inner:
            return inner
    return loc


def _coords_from_browser_location(loc: Any) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """
    streamlit_geolocation / get_geolocation 응답에서 (lat, lng, error_message).
    error_message 는 브라우저가 거부했을 때만 설정; 대기 중이면 모두 None.
    """
    loc = _unwrap_js_eval_location(loc)
    if loc is None or not isinstance(loc, dict):
        return None, None, None
    err = loc.get("error")
    if err:
        if isinstance(err, dict):
            code = err.get("code")
            msg = err.get("message") or "위치를 가져오지 못했습니다"
            return None, None, f"{msg} (code: {code})"
        return None, None, str(err)
    coords = loc.get("coords")
    if isinstance(coords, dict):
        la, ln = coords.get("latitude"), coords.get("longitude")
        if la is not None and ln is not None:
            try:
                return float(la), float(ln), None
            except (TypeError, ValueError):
                pass
    la, ln = loc.get("latitude"), loc.get("longitude")
    if la is not None and ln is not None:
        try:
            return float(la), float(ln), None
        except (TypeError, ValueError):
            pass
    return None, None, None


def _user_agent() -> str:
    return os.getenv("DINING_APP_USER_AGENT", "ojomwo-app/0.1")


@st.cache_data(ttl=60 * 60, show_spinner=False)
def cached_ip_location() -> tuple[float, float]:
    """
    IP 기반 대략 위치:
    - API 키 없이 대략적인 위/경도 제공 (정확도는 낮을 수 있음)
    """
    headers = {"User-Agent": _user_agent()}
    resp = requests.get("https://ipapi.co/json/", headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    lat = data.get("latitude")
    lng = data.get("longitude")
    if lat is None or lng is None:
        raise RuntimeError("ip location failed: latitude/longitude missing")
    return float(lat), float(lng)


@st.cache_data(ttl=60 * 60, show_spinner=False)
def cached_geocode(address: str) -> tuple[float, float]:
    return geocode_address(address)


@st.cache_data(ttl=60 * 60, show_spinner=False)
def cached_places(
    lat: float,
    lng: float,
    radius_m: int,
    query: str,
    limit: int,
    provider_preference: str,
    location_text: Optional[str],
    api_key_fingerprint: str,
) -> list[dict[str, Any]]:
    return search_nearby_places(
        lat=lat,
        lng=lng,
        radius_m=radius_m,
        query=query,
        limit=limit,
        provider_preference=provider_preference,
        location_text=location_text,
    )


def _normalize_rating_text(s: str) -> Optional[float]:
    if not s:
        return None
    t = str(s).strip().replace(",", ".")
    try:
        v = float(t)
    except Exception:
        return None
    # 일반적으로 0~5 범위
    if v < 0 or v > 10:
        return None
    return v


def _extract_kakao_rating_from_html(html_text: str) -> Optional[float]:
    if not html_text:
        return None

    # JSON-LD 등에서 흔히 보이는 키워드
    patterns = [
        r'"ratingValue"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        r'"starRating"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        r'itemprop=["\']ratingValue["\']\s*content=["\']([^"\']+)["\']',
        r'"rating"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
    ]
    for p in patterns:
        m = re.search(p, html_text, flags=re.IGNORECASE)
        if m:
            return _normalize_rating_text(m.group(1))
    return None


@st.cache_data(ttl=60 * 60, show_spinner=False)
def cached_kakao_place_meta(place_url: str) -> tuple[Optional[str], Optional[float]]:
    """
    Kakao place 페이지에서
    - 대표 이미지(OG:meta og:image)
    - 별점(ratingValue 등)
    를 최대한 파싱해 반환합니다.
    """
    u = (place_url or "").strip()
    if not u:
        return None, None

    headers = {"User-Agent": _user_agent()}
    try:
        resp = requests.get(u, headers=headers, timeout=20)
        resp.raise_for_status()
    except Exception:
        return None, None

    page = resp.text or ""

    img_match = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        page,
        flags=re.IGNORECASE,
    )
    image_url: Optional[str] = None
    if img_match:
        image_url = html.unescape(img_match.group(1)).strip()
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"
        if not image_url:
            image_url = None

    rating = _extract_kakao_rating_from_html(page)
    return image_url, rating


def cached_place_image_url(place_url: str) -> Optional[str]:
    # 기존 호출부 호환용 래퍼
    img, _rating = cached_kakao_place_meta(place_url)
    return img


def _google_maps_q_url(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lng}"


def _stable_seed(*parts: Any) -> int:
    raw = json.dumps(parts, ensure_ascii=True, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return int(digest, 16)


def _fingerprint_key(api_key: str) -> str:
    """
    캐시 파라미터에 키 원문을 넣지 않기 위해 해시 지문만 사용합니다.
    """
    raw = api_key.strip()
    if not raw:
        return ""
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return digest


def _get_kakao_api_key() -> str:
    """
    - 우선: 환경변수 `KAKAO_REST_API_KEY`
    - 차선: Streamlit secrets(`.streamlit/secrets.toml`)의 `KAKAO_REST_API_KEY`
    """
    env_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    if env_key:
        return env_key

    # st.secrets는 Streamlit 실행 맥락에서만 안정적으로 접근됩니다.
    try:
        secret_val = st.secrets.get("KAKAO_REST_API_KEY")  # type: ignore[attr-defined]
        if secret_val:
            return str(secret_val).strip()
    except Exception:
        pass

    try:
        secret_val = st.secrets["KAKAO_REST_API_KEY"]  # type: ignore[attr-defined]
        if secret_val:
            return str(secret_val).strip()
    except Exception:
        pass

    return ""


def _get_kakao_js_key() -> str:
    """
    Kakao Maps JavaScript SDK용 키.
    - Streamlit secrets(`.streamlit/secrets.toml`)의 `KAKAO_JAVASCRIPT_KEY`를 우선 사용
    - (옵션) 환경변수 `KAKAO_JAVASCRIPT_KEY`
    """
    env_key = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
    if env_key:
        return env_key
    try:
        v = st.secrets.get("KAKAO_JAVASCRIPT_KEY")  # type: ignore[attr-defined]
        if v:
            return str(v).strip()
    except Exception:
        pass
    try:
        v = st.secrets["KAKAO_JAVASCRIPT_KEY"]  # type: ignore[attr-defined]
        if v:
            return str(v).strip()
    except Exception:
        pass
    return ""


def _candidate_key(c: dict[str, Any]) -> str:
    # external_id가 있는 provider(주로 google/kakao)면 그걸 우선합니다.
    ext = c.get("external_id") or ""
    lat = c.get("lat")
    lng = c.get("lng")
    name = c.get("name") or ""
    address = c.get("address") or ""
    return f"{ext}|{name}|{address}|{lat}|{lng}"


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in candidates:
        k = _candidate_key(c)
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def _get_distance_m(c: dict[str, Any]) -> Optional[float]:
    d = c.get("distance_m")
    if d is None:
        return None
    try:
        return float(d)
    except Exception:
        return None


def _weighted_sample_without_replacement(
    rng: random.Random,
    items: list[dict[str, Any]],
    weights: list[float],
    k: int,
) -> list[dict[str, Any]]:
    if k <= 0 or not items:
        return []

    items_rem = list(items)
    weights_rem = list(weights)

    out: list[dict[str, Any]] = []
    for _ in range(min(k, len(items_rem))):
        total = sum(weights_rem)
        if total <= 0:
            idx = rng.randrange(0, len(items_rem))
        else:
            r = rng.random() * total
            acc = 0.0
            idx = 0
            for i, w in enumerate(weights_rem):
                acc += w
                if r <= acc:
                    idx = i
                    break

        out.append(items_rem.pop(idx))
        weights_rem.pop(idx)
    return out


def _pick_with_distance_preference(
    candidates: list[dict[str, Any]],
    *,
    pick_count: int,
    top_percent: float,
    weighted_by_distance: bool,
    round_no: int,
    params_signature: str,
)-> tuple[list[dict[str, Any]], bool]:
    """
    - distance_m가 있는 후보를 가까운 순으로 정렬한 뒤,
      top_percent(상위 %) 구간에서 추천합니다.
    - weighted_by_distance=True면 (가까울수록 더 높은 확률)로 랜덤 추출합니다.
    - 반환: (picks, used_distance_filter)
    """
    candidates = _dedupe_candidates(candidates)

    if not candidates:
        return [], False

    has_any_distance = any(_get_distance_m(c) is not None for c in candidates)

    enriched: list[tuple[float, dict[str, Any]]] = []
    for c in candidates:
        d = _get_distance_m(c)
        enriched.append((d if d is not None else 1e18, c))

    enriched.sort(key=lambda x: x[0])

    top_percent = max(1.0, min(100.0, float(top_percent)))
    top_size = max(1, int(math.ceil(len(enriched) * top_percent / 100.0)))
    eligible = [c for _d, c in enriched[:top_size]]

    used_distance_filter = has_any_distance and top_size < len(candidates)

    rng = random.Random(_stable_seed(date.today().isoformat(), round_no, params_signature, "distance_pick"))

    if not weighted_by_distance:
        return rng.sample(eligible, k=min(pick_count, len(eligible))), used_distance_filter

    weights: list[float] = []
    any_real_distance = any(_get_distance_m(c) is not None for c in eligible)
    for c in eligible:
        d = _get_distance_m(c)
        if d is None or d >= 1e17:
            weights.append(1.0 if not any_real_distance else 1e-9)
        else:
            weights.append(1.0 / (d + 1.0))

    return _weighted_sample_without_replacement(rng, eligible, weights, k=pick_count), used_distance_filter


def _eligible_candidates_by_distance(
    candidates: list[dict[str, Any]],
    *,
    top_percent: float,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Kakao 응답에 distance_m이 있으면 가까운 순으로 정렬한 뒤,
    상위 top_percent 구간의 후보만 반환합니다.
    """
    candidates = _dedupe_candidates(candidates)
    if not candidates:
        return [], False

    has_any_distance = any(_get_distance_m(c) is not None for c in candidates)

    enriched: list[tuple[float, dict[str, Any]]] = []
    for c in candidates:
        d = _get_distance_m(c)
        enriched.append((d if d is not None else 1e18, c))
    enriched.sort(key=lambda x: x[0])

    top_percent = max(1.0, min(100.0, float(top_percent)))
    top_size = max(1, int(math.ceil(len(enriched) * top_percent / 100.0)))
    eligible = [c for _d, c in enriched[:top_size]]
    used_distance_filter = has_any_distance and len(eligible) < len(candidates)
    return eligible, used_distance_filter


def _pick_final_one(
    picks: list[dict[str, Any]],
    *,
    round_no: int,
    params_signature: str,
) -> Optional[dict[str, Any]]:
    if not picks:
        return None
    rng = random.Random(_stable_seed(date.today().isoformat(), round_no, params_signature, "final_one"))
    return picks[rng.randrange(0, len(picks))]


def _pick_with_optional_kakao_rating_filter(
    *,
    candidates: list[dict[str, Any]],
    pick_count: int,
    top_percent: float,
    weighted_by_distance: bool,
    round_no: int,
    params_signature: str,
    rating_on: bool,
    rating_min: float,
    rating_check_limit: int,
    rating_fallback_to_distance_random: bool,
) -> tuple[list[dict[str, Any]], bool, bool, int]:
    """
    반환:
    - picks
    - used_distance_filter
    - used_rating_filter
    - rating_good_count
    """
    eligible_distance, used_distance_filter = _eligible_candidates_by_distance(
        candidates,
        top_percent=top_percent,
    )

    # 별점 모드 off => 기존 거리 로직 그대로 사용
    if not rating_on:
        picks, used_distance_filter2 = _pick_with_distance_preference(
            candidates,
            pick_count=pick_count,
            top_percent=top_percent,
            weighted_by_distance=weighted_by_distance,
            round_no=round_no,
            params_signature=params_signature,
        )
        return picks, used_distance_filter2, False, 0

    if not eligible_distance:
        return [], used_distance_filter, False, 0

    # 가까운 것부터 최대 rating_check_limit개만 스크래핑
    to_check = eligible_distance[: max(1, int(rating_check_limit))]

    rating_good: list[dict[str, Any]] = []
    used_rating_filter = False

    for c in to_check:
        kakao_place_url = _to_kakao_place_url(c)
        if not kakao_place_url:
            continue
        _image_url, rating = cached_kakao_place_meta(kakao_place_url)
        c["image_url"] = _image_url
        c["rating"] = rating
        if rating is not None and float(rating) >= float(rating_min):
            rating_good.append(c)
    rating_good_count = len(rating_good)

    if rating_good_count <= 0:
        if rating_fallback_to_distance_random:
            picks, used_distance_filter2 = _pick_with_distance_preference(
                candidates,
                pick_count=pick_count,
                top_percent=top_percent,
                weighted_by_distance=weighted_by_distance,
                round_no=round_no,
                params_signature=params_signature,
            )
            return picks, used_distance_filter2, False, 0
        return [], used_distance_filter, False, 0

    used_rating_filter = True

    rng = random.Random(_stable_seed(date.today().isoformat(), round_no, params_signature, "rating_good_pick"))

    # 1) rating_good에서 먼저 필요한 만큼 뽑기
    k_first = min(pick_count, rating_good_count)
    if weighted_by_distance:
        weights = [1.0 / ((_get_distance_m(c) or 1e18) + 1.0) for c in rating_good]
        picks = _weighted_sample_without_replacement(rng, rating_good, weights, k=k_first)
    else:
        picks = rng.sample(rating_good, k=k_first)

    # 2) 부족하면 fallback으로 distance eligible에서 보충
    if len(picks) < pick_count and rating_fallback_to_distance_random:
        picked_keys = {_candidate_key(c) for c in picks}
        remaining = [c for c in eligible_distance if _candidate_key(c) not in picked_keys]
        need = pick_count - len(picks)
        if remaining and need > 0:
            if weighted_by_distance:
                weights = [1.0 / ((_get_distance_m(c) or 1e18) + 1.0) for c in remaining]
                picks.extend(_weighted_sample_without_replacement(rng, remaining, weights, k=min(need, len(remaining))))
            else:
                picks.extend(rng.sample(remaining, k=min(need, len(remaining))))

    return picks, used_distance_filter, used_rating_filter, rating_good_count


def _to_kakao_place_url(c: dict[str, Any]) -> str:
    place_url = str(c.get("place_url") or "").strip()
    if place_url:
        return place_url
    ext = str(c.get("external_id") or "").strip()
    if ext.isdigit():
        return f"https://place.map.kakao.com/{ext}"
    return ""


def _render_map(
    *,
    user_lat: float,
    user_lng: float,
    candidates: list[dict[str, Any]],
    final_pick: Optional[dict[str, Any]],
    radius_m: int,
) -> None:
    cand_rows: list[dict[str, Any]] = []
    final_key = _candidate_key(final_pick) if final_pick else ""
    for c in candidates:
        lat = c.get("lat")
        lng = c.get("lng")
        if lat is None or lng is None:
            continue
        try:
            clat = float(lat)
            clng = float(lng)
        except Exception:
            continue
        is_final = _candidate_key(c) == final_key
        cand_rows.append(
            {
                "lat": clat,
                "lng": clng,
                "name": str(c.get("name") or "Unknown"),
                "distance_m": int(float(c.get("distance_m") or 0)),
                "color": [231, 76, 60, 220] if is_final else [52, 152, 219, 170],
                "radius": 12 if is_final else 8,  # pixels
            }
        )

    if not cand_rows:
        st.info("지도에 표시할 후보 좌표가 없습니다.")
        return

    cand_df = pd.DataFrame(cand_rows)
    user_df = pd.DataFrame([{"lat": float(user_lat), "lng": float(user_lng)}])

    layers = [
        pdk.Layer(
            "ScatterplotLayer",
            data=cand_df,
            get_position="[lng, lat]",
            get_fill_color="color",
            get_radius="radius",
            radius_units="pixels",
            pickable=True,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=user_df,
            get_position="[lng, lat]",
            get_fill_color=[46, 204, 113, 230],
            get_radius=14,
            radius_units="pixels",
            pickable=False,
        ),
    ]

    center_lat = float(final_pick.get("lat")) if final_pick and final_pick.get("lat") is not None else float(user_lat)
    center_lng = float(final_pick.get("lng")) if final_pick and final_pick.get("lng") is not None else float(user_lng)

    # 반경 기반 줌(휴리스틱): IP 위치는 부정확할 수 있어 너무 확대하지 않게 상한/하한을 둠
    r = max(300, int(radius_m))
    if r <= 600:
        zoom = 15.8
    elif r <= 1200:
        zoom = 15.2
    elif r <= 2000:
        zoom = 14.6
    elif r <= 3500:
        zoom = 14.0
    elif r <= 5500:
        zoom = 13.4
    else:
        zoom = 12.8

    deck = pdk.Deck(
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lng, zoom=zoom),
        layers=layers,
        tooltip={"text": "{name}\n거리: {distance_m}m"},
    )
    st.pydeck_chart(deck, use_container_width=True)


def _render_kakao_map(
    *,
    user_lat: float,
    user_lng: float,
    candidates: list[dict[str, Any]],
    final_pick: Optional[dict[str, Any]],
    height_px: int = 520,
) -> bool:
    """
    Kakao Maps JS SDK를 Streamlit에 임베드합니다.
    - 성공적으로 렌더링 요청을 보냈으면 True, 키가 없으면 False
    """
    js_key = _get_kakao_js_key()
    if not js_key:
        return False

    final_key = _candidate_key(final_pick) if final_pick else ""
    points: list[dict[str, Any]] = []
    for c in candidates:
        lat = c.get("lat")
        lng = c.get("lng")
        if lat is None or lng is None:
            continue
        try:
            clat = float(lat)
            clng = float(lng)
        except Exception:
            continue
        points.append(
            {
                "lat": clat,
                "lng": clng,
                "name": str(c.get("name") or "Unknown"),
                "isFinal": _candidate_key(c) == final_key,
            }
        )

    # HTML/JS (kakao maps)
    # NOTE: appkey는 JavaScript 키여야 하며, localhost/배포 도메인이 카카오 콘솔에 등록되어야 합니다.
    payload = json.dumps(
        {
            "user": {"lat": float(user_lat), "lng": float(user_lng)},
            "points": points,
        },
        ensure_ascii=False,
    )

    html_doc = f"""
    <div id="kakao-map" style="width: 100%; height: {int(height_px)}px; border-radius: 12px; overflow: hidden;"></div>
    <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={js_key}&autoload=false"></script>
    <script>
      const data = {payload};
      kakao.maps.load(function() {{
        const container = document.getElementById('kakao-map');
        const center = new kakao.maps.LatLng(data.user.lat, data.user.lng);
        const map = new kakao.maps.Map(container, {{
          center: center,
          level: 4
        }});

        const bounds = new kakao.maps.LatLngBounds();
        bounds.extend(center);

        // user marker (green)
        const userMarker = new kakao.maps.Marker({{
          position: center
        }});
        userMarker.setMap(map);

        // points markers
        data.points.forEach(p => {{
          const pos = new kakao.maps.LatLng(p.lat, p.lng);
          bounds.extend(pos);

          const marker = new kakao.maps.Marker({{
            position: pos
          }});
          marker.setMap(map);

          const content = `<div style="padding:6px 8px;font-size:12px;max-width:240px;">
            <b>${{p.isFinal ? '⭐ ' : ''}}${{p.name}}</b>
          </div>`;
          const infowindow = new kakao.maps.InfoWindow({{
            content: content
          }});
          kakao.maps.event.addListener(marker, 'click', function() {{
            infowindow.open(map, marker);
          }});
        }});

        if (data.points.length > 0) {{
          map.setBounds(bounds);
        }}
      }});
    </script>
    """

    components.html(html_doc, height=int(height_px) + 8)
    return True


def _render_pick_card(c: dict[str, Any], idx: int, *, show_distance: bool, show_address: bool) -> None:
    name = str(c.get("name") or "Unknown")
    addr = str(c.get("address") or "")
    lat = c.get("lat")
    lng = c.get("lng")
    distance_m = c.get("distance_m")
    distance_str = "-"
    if distance_m is not None:
        try:
            distance_str = f"{int(float(distance_m))}m"
        except Exception:
            distance_str = "-"

    rating = c.get("rating")
    rating_str = "-"
    if rating is not None:
        try:
            rating_str = f"{float(rating):.1f}"
        except Exception:
            rating_str = "-"

    url = _google_maps_q_url(float(lat), float(lng)) if lat is not None and lng is not None else ""

    chip_distance = f"<span class='chip'>📍 {distance_str}</span>" if (show_distance and distance_str != "-") else ""
    chip_rating = f"<span class='chip'>⭐ {rating_str}</span>" if rating_str != "-" else "<span class='chip'>⭐ -</span>"
    chip_addr = f"<div class='muted' style='margin-top:6px'>{html.escape(addr)}</div>" if (show_address and addr) else ""
    map_link = f"<a class='btn-link' href='{url}' target='_blank' rel='noopener'>지도 열기</a>" if url else ""

    st.markdown(
        f"""
        <div class="card">
          <div class="card-top">
            <div class="kicker">추천 #{idx}</div>
            <div class="title">{html.escape(name)}</div>
            <div class="chips">
              {chip_rating}
              {chip_distance}
            </div>
            {chip_addr}
          </div>
          <div class="card-bottom">
            {map_link}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="오점뭐", page_icon="🍽️", layout="wide")

    st.markdown(
        """
        <style>
          /* Layout */
          .block-container { padding-top: 1.2rem; padding-bottom: 2.0rem; }
          [data-testid="stSidebar"] { border-right: 1px solid rgba(0,0,0,0.06); }
          [data-testid="stSidebar"] > div:first-child { background: linear-gradient(180deg, #f8fbff 0%, #f7f7ff 42%, #ffffff 100%); }
          [data-testid="stSidebar"] .block-container { padding-top: 1rem; }
          [data-testid="stSidebar"] h2 { font-size: 1.15rem; letter-spacing: -0.01em; }
          [data-testid="stSidebar"] h3 { font-size: 1rem; letter-spacing: -0.01em; color: #334155; margin-top: 0.75rem; }
          [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: rgba(15,23,42,0.72); }
          [data-testid="stSidebar"] [data-testid="stDivider"] { margin: 0.7rem 0 0.85rem 0; }
          [data-testid="stSidebar"] .stSelectbox > div,
          [data-testid="stSidebar"] .stTextInput > div > div,
          [data-testid="stSidebar"] .stMultiSelect > div > div,
          [data-testid="stSidebar"] .stNumberInput > div > div {
            border-radius: 12px;
          }
          .side-card { border: 1px solid rgba(37,99,235,0.15); background: linear-gradient(135deg, rgba(37,99,235,0.08), rgba(168,85,247,0.06)); border-radius: 14px; padding: 10px 12px; margin-bottom: 10px; }
          .side-card .title { font-weight: 800; font-size: 13px; }
          .side-card .desc { color: rgba(15,23,42,0.68); font-size: 12px; margin-top: 4px; }

          /* Typography */
          .hero { padding: 18px 18px; border-radius: 22px; background: linear-gradient(135deg, rgba(37,99,235,0.10), rgba(168,85,247,0.08), rgba(34,197,94,0.06)); border: 1px solid rgba(15,23,42,0.10); }
          .hero-top { display:flex; align-items:center; justify-content:space-between; gap:12px; }
          .badge { display:inline-flex; align-items:center; gap:6px; padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,0.85); border: 1px solid rgba(15,23,42,0.10); font-size: 12px; font-weight: 800; color: rgba(15,23,42,0.78); }
          .hero h1 { margin: 8px 0 0 0; font-size: 34px; line-height: 1.12; letter-spacing: -0.03em; }
          .hero p { margin: 8px 0 0 0; color: rgba(15,23,42,0.72); }
          .section-title { margin-top: 14px; margin-bottom: 6px; font-size: 18px; font-weight: 800; letter-spacing: -0.01em; }
          .muted { color: rgba(15,23,42,0.60); font-size: 13px; }

          /* Cards */
          .card { background: #FFFFFF; border: 1px solid rgba(15,23,42,0.10); border-radius: 16px; padding: 14px 14px; box-shadow: 0 10px 24px rgba(15,23,42,0.06); margin-bottom: 16px; }
          .card-top { }
          .kicker { font-size: 12px; color: rgba(15,23,42,0.55); }
          .title { font-size: 18px; font-weight: 800; margin-top: 4px; letter-spacing: -0.01em; }
          .chips { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
          .chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 9px; border-radius: 999px; border: 1px solid rgba(15,23,42,0.10); background: rgba(246,247,251,0.95); font-size: 12px; }
          .card-bottom { margin-top: 12px; display:flex; justify-content:flex-end; }
          .btn-link { text-decoration: none; font-weight: 800; color: #2563EB; }

          /* Final highlight */
          .final { border-radius: 18px; padding: 16px 16px; background: rgba(15,23,42,0.03); border: 1px solid rgba(15,23,42,0.10); }
          .final .final-title { font-size: 16px; font-weight: 900; margin: 0 0 6px 0; }
          .final .final-name { font-size: 22px; font-weight: 900; margin: 0; letter-spacing: -0.01em; }

          /* Buttons spacing */
          div.stButton > button { border-radius: 12px; padding: 0.6rem 0.9rem; font-weight: 800; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="hero">
          <div class="hero-top">
            <span class="badge">🍱 오늘 점심 뭐먹지?</span>
            <span class="badge">🎲 랜덤 추천</span>
          </div>
          <h1>오점뭐</h1>
          <p>고민은 가볍게. <b>오늘 한 곳만</b> 편하게 골라드릴게요.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 위치는 세션에서 한 번만 확정: GPS 우선, 실패 시 IP 자동 폴백
    if "fixed_user_lat" not in st.session_state:
        st.session_state["fixed_user_lat"] = None
    if "fixed_user_lng" not in st.session_state:
        st.session_state["fixed_user_lng"] = None
    if "fixed_user_source" not in st.session_state:
        st.session_state["fixed_user_source"] = ""

    # 위치 고정 규칙:
    # - 아직 좌표가 없으면 GPS 우선 -> 실패 시 IP로 임시 설정
    # - 이미 IP로 고정된 상태면, 이후 렌더에서 GPS를 조용히 재시도해 성공 시 업그레이드
    need_initial_fix = st.session_state["fixed_user_lat"] is None or st.session_state["fixed_user_lng"] is None
    try_upgrade_to_gps = st.session_state.get("fixed_user_source") != "gps"

    if need_initial_fix or try_upgrade_to_gps:
        gps_lat: Optional[float] = None
        gps_lng: Optional[float] = None
        gps_err: Optional[str] = None
        # streamlit_geolocation 컴포넌트는 화면에 버튼 UI를 렌더링할 수 있어
        # 기본은 get_geolocation(js-eval) 경로만 사용합니다.
        if get_geolocation is not None:
            loc = get_geolocation(component_key="ojomwo_gps_main")
            gps_lat, gps_lng, gps_err = _coords_from_browser_location(loc)
        elif streamlit_geolocation is not None:
            loc = streamlit_geolocation()
            gps_lat, gps_lng, gps_err = _coords_from_browser_location(loc)

        if gps_lat is not None and gps_lng is not None:
            st.session_state["fixed_user_lat"] = float(gps_lat)
            st.session_state["fixed_user_lng"] = float(gps_lng)
            st.session_state["fixed_user_source"] = "gps"
        elif need_initial_fix:
            try:
                ip_lat, ip_lng = cached_ip_location()
                st.session_state["fixed_user_lat"] = float(ip_lat)
                st.session_state["fixed_user_lng"] = float(ip_lng)
                st.session_state["fixed_user_source"] = "ip"
            except Exception:
                # 위치가 끝내 비어 있으면 추천 시점에서 다시 방어적으로 처리합니다.
                if gps_err:
                    st.caption("위치를 아직 못 받아서 잠시 기다리는 중이에요.")

    fixed_lat = st.session_state.get("fixed_user_lat")
    fixed_lng = st.session_state.get("fixed_user_lng")

    with st.sidebar:
        st.header("설정")
        st.markdown(
            """
            <div class="side-card">
              <div class="title">✨ 오늘 점심 취향 맞추기</div>
              <div class="desc">왼쪽에서 취향만 고르면, 추천은 더 가볍게 해드릴게요.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.subheader("🗺️ 장소 데이터")
        kakao_env_key = _get_kakao_api_key()
        # place_provider는 os.getenv를 보기 때문에, secrets로부터 읽었다면 환경변수에도 주입합니다.
        if kakao_env_key and not os.getenv("KAKAO_REST_API_KEY"):
            os.environ["KAKAO_REST_API_KEY"] = kakao_env_key
        provider_preference = "kakao"
        st.write("장소는 `kakao` 데이터로 찾고 있어요.")
        if not kakao_env_key:
            st.error("`KAKAO_REST_API_KEY`가 없어서 실제 장소를 불러오지 못하고 있어요.")

        st.subheader("🍜 음식 종류")
        categories = st.multiselect(
            "먹고 싶은 종류(여러 개 선택 가능)",
            options=["전체", "한식", "중식", "일식", "카페", "베이커리"],
            default=["전체"],
        )
        extra_query = st.text_input("원하는 메뉴 키워드(선택)", value="", placeholder="예: 국밥, 파스타, 브런치")

        # Kakao keyword search query 구성
        parts: list[str] = []
        # '전체'가 선택되면 다른 카테고리는 무시하고 전반 검색으로 처리
        if "전체" in categories:
            parts.append("맛집")
        else:
            parts.extend([c.strip() for c in categories if str(c).strip()])
        if extra_query.strip():
            parts.append(extra_query.strip())
        if not parts:
            parts = ["맛집"]
        query = " ".join(parts)
        st.caption(f"이렇게 찾아볼게요: `{query}`")
        radius_m = st.slider("어디까지 찾아볼까요? (m)", min_value=300, max_value=8000, value=1500, step=100)
        candidate_pool = st.slider("후보 개수", min_value=5, max_value=60, value=25, step=1)

        st.divider()
        pick_count = st.slider("추천 받을 개수", min_value=2, max_value=8, value=5, step=1)

        show_distance = st.checkbox("거리 함께 보기", value=True)
        show_address = st.checkbox("주소 함께 보기", value=False)

        st.divider()
        st.subheader("⭐ 별점 기준")
        rating_on = st.checkbox("별점 높은 곳을 우선 추천", value=True)
        rating_min = st.slider("최소 별점", min_value=0.0, max_value=5.0, value=4.0, step=0.1)
        rating_check_limit = st.slider("별점 확인할 후보 수", min_value=5, max_value=30, value=15, step=1)
        rating_fallback_to_distance_random = st.checkbox(
            "조건에 맞는 곳이 없으면 거리 기준으로도 추천받기",
            value=True,
        )

        st.divider()
        st.subheader("🎯 추천 방식")
        top_percent = st.slider("가까운 곳 우선 범위(상위 %)", min_value=10, max_value=100, value=40, step=5)
        weighted_by_distance = st.checkbox("가까운 곳이 조금 더 잘 나오게", value=True)

        st.divider()
        st.subheader("🎡 룰렛 연출")
        roulette_on = st.checkbox("룰렛 느낌 보기", value=True)
        roulette_rounds = st.slider("룰렛 돌리는 횟수", min_value=6, max_value=30, value=14, step=1)

    # session_state init
    if "round_no" not in st.session_state:
        st.session_state["round_no"] = 0
    if "last_signature" not in st.session_state:
        st.session_state["last_signature"] = ""
    if "last_candidates" not in st.session_state:
        st.session_state["last_candidates"] = []
    if "last_picks" not in st.session_state:
        st.session_state["last_picks"] = []
    if "last_used_distance_filter" not in st.session_state:
        st.session_state["last_used_distance_filter"] = False
    if "last_final_pick" not in st.session_state:
        st.session_state["last_final_pick"] = None
    if "last_user_lat" not in st.session_state:
        st.session_state["last_user_lat"] = None
    if "last_user_lng" not in st.session_state:
        st.session_state["last_user_lng"] = None
    if "last_used_rating_filter" not in st.session_state:
        st.session_state["last_used_rating_filter"] = False
    if "last_rating_good_count" not in st.session_state:
        st.session_state["last_rating_good_count"] = 0

    params_signature = _stable_seed(
        "gps_auto",
        float(fixed_lat) if fixed_lat is not None else None,
        float(fixed_lng) if fixed_lng is not None else None,
        provider_preference,
        query.strip(),
        tuple(categories),
        extra_query.strip(),
        int(radius_m),
        int(candidate_pool),
        int(pick_count),
        float(top_percent),
        bool(weighted_by_distance),
        bool(rating_on),
        float(rating_min),
        int(rating_check_limit),
        bool(rating_fallback_to_distance_random),
    )
    params_signature_str = str(params_signature)

    st.markdown("<div class='section-title'>추천 받기</div>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1.2, 0.9, 0.9])
    with c1:
        draw_btn = st.button("오늘 점심 골라줘", type="primary")
    with c2:
        reroll_btn = st.button("다른 추천 보기", disabled=not st.session_state.get("last_candidates"))
    with c3:
        st.caption("누를 때마다 다른 조합으로 보여드려요.")

    def ensure_candidates() -> tuple[list[dict[str, Any]], Optional[str]]:
        nonlocal params_signature_str

        kakao_key_now = _get_kakao_api_key()
        if not kakao_key_now:
            st.error("카카오 API 키가 아직 없어요. `.streamlit/secrets.toml` 또는 환경변수를 확인해주세요.")
            st.stop()

        if st.session_state.get("last_signature") == params_signature_str and st.session_state.get("last_candidates"):
            return st.session_state["last_candidates"], None

        with st.spinner("근처 장소를 찾는 중이에요..."):
            try:
                if fixed_lat is not None and fixed_lng is not None:
                    lat, lng = float(fixed_lat), float(fixed_lng)
                else:
                    # 극단적 예외 상황에서만 위치를 다시 시도
                    lat, lng = cached_ip_location()

                location_text_for_naver = None
            except Exception as e:
                st.exception(e)
                st.error("위치 정보를 가져오지 못했어요. 네트워크 상태를 확인해주세요.")
                st.stop()

            try:
                api_key_effective = _get_kakao_api_key()
                if api_key_effective and not os.getenv("KAKAO_REST_API_KEY"):
                    os.environ["KAKAO_REST_API_KEY"] = api_key_effective
                api_key_fingerprint = _fingerprint_key(api_key_effective)
                candidates = cached_places(
                    lat=lat,
                    lng=lng,
                    radius_m=int(radius_m),
                    query=query.strip() or "맛집",
                    limit=int(candidate_pool),
                    provider_preference=provider_preference,
                    location_text=location_text_for_naver,
                    api_key_fingerprint=api_key_fingerprint,
                )
            except Exception as e:
                msg = str(e)
                if "disabled OPEN_MAP_AND_LOCAL service" in msg:
                    st.error(
                        "카카오 앱에서 `OPEN_MAP_AND_LOCAL` 서비스가 비활성화되어 있습니다. "
                        "Kakao Developers 콘솔에서 Local/지도 서비스를 활성화해 주세요."
                    )
                    st.info(
                        "설정 후 1~2분 정도 기다린 뒤 다시 시도해보세요. "
                        "경로: 내 애플리케이션 -> 제품 설정 -> 카카오맵(로컬) 관련 서비스 활성화"
                    )
                else:
                    st.exception(e)
                    st.error("근처 장소를 찾지 못했어요. API 키나 네트워크 상태를 확인해주세요.")
                st.stop()

        st.session_state["last_candidates"] = candidates
        st.session_state["last_user_lat"] = lat
        st.session_state["last_user_lng"] = lng
        st.session_state["last_signature"] = params_signature_str
        return candidates, f"lat={lat}, lng={lng}"

    if draw_btn:
        st.session_state["round_no"] = int(st.session_state.get("round_no") or 0) + 1
        candidates, _loc = ensure_candidates()

        picks, used_distance_filter2, used_rating_filter2, rating_good_count2 = _pick_with_optional_kakao_rating_filter(
            candidates=candidates,
            pick_count=int(pick_count),
            top_percent=float(top_percent),
            weighted_by_distance=bool(weighted_by_distance),
            round_no=int(st.session_state["round_no"]),
            params_signature=params_signature_str,
            rating_on=bool(rating_on),
            rating_min=float(rating_min),
            rating_check_limit=int(rating_check_limit),
            rating_fallback_to_distance_random=bool(rating_fallback_to_distance_random),
        )
        st.session_state["last_picks"] = picks
        st.session_state["last_used_distance_filter"] = used_distance_filter2
        st.session_state["last_used_rating_filter"] = used_rating_filter2
        st.session_state["last_rating_good_count"] = int(rating_good_count2)
        st.session_state["last_final_pick"] = _pick_final_one(
            picks,
            round_no=int(st.session_state["round_no"]),
            params_signature=params_signature_str,
        )

    if reroll_btn and not draw_btn:
        # 같은 조건에서만 리롤(후보/조건이 바뀌면 버튼을 눌러도 candidates가 다시 로드됨)
        st.session_state["round_no"] = int(st.session_state.get("round_no") or 0) + 1
        candidates, _loc = ensure_candidates()

        picks, used_distance_filter2, used_rating_filter2, rating_good_count2 = _pick_with_optional_kakao_rating_filter(
            candidates=candidates,
            pick_count=int(pick_count),
            top_percent=float(top_percent),
            weighted_by_distance=bool(weighted_by_distance),
            round_no=int(st.session_state["round_no"]),
            params_signature=params_signature_str,
            rating_on=bool(rating_on),
            rating_min=float(rating_min),
            rating_check_limit=int(rating_check_limit),
            rating_fallback_to_distance_random=bool(rating_fallback_to_distance_random),
        )
        st.session_state["last_picks"] = picks
        st.session_state["last_used_distance_filter"] = used_distance_filter2
        st.session_state["last_used_rating_filter"] = used_rating_filter2
        st.session_state["last_rating_good_count"] = int(rating_good_count2)
        st.session_state["last_final_pick"] = _pick_final_one(
            picks,
            round_no=int(st.session_state["round_no"]),
            params_signature=params_signature_str,
        )

    if st.session_state.get("last_picks"):
        picks: list[dict[str, Any]] = st.session_state["last_picks"]
        st.markdown("<div class='section-title'>추천 결과</div>", unsafe_allow_html=True)

        distance_filter_msg = (
            "가까운 곳을 우선으로 골라봤어요."
            if st.session_state.get("last_used_distance_filter")
            else "거리 정보가 부족해 전체 후보에서 고르게 골라봤어요."
        )
        if st.session_state.get("last_used_rating_filter"):
            st.caption(
                f"{distance_filter_msg} (별점 4점 이상 스크래핑 필터 적용: {st.session_state.get('last_rating_good_count', 0)}개 후보)"
            )
        else:
            st.caption(distance_filter_msg)

        # 0개면 안내만 표시
        if not picks:
            st.warning("조건에 맞는 장소를 찾지 못했어요. 키워드나 반경을 조금 바꿔보세요.")
            return

        final_pick = st.session_state.get("last_final_pick")
        if final_pick:
            final_name = str(final_pick.get("name") or "Unknown")
            final_addr = str(final_pick.get("address") or "")
            final_d = _get_distance_m(final_pick)
            final_d_str = f"{int(final_d)}m" if final_d is not None else "-"
            final_lat = final_pick.get("lat")
            final_lng = final_pick.get("lng")
            final_url = _google_maps_q_url(float(final_lat), float(final_lng)) if final_lat is not None and final_lng is not None else ""
            kakao_place_url = _to_kakao_place_url(final_pick)
            if bool(rating_on) and final_pick.get("rating") is None and kakao_place_url:
                _img2, _rating2 = cached_kakao_place_meta(kakao_place_url)
                final_pick["image_url"] = _img2
                final_pick["rating"] = _rating2

            rating_val = final_pick.get("rating")
            rating_val_str = "-"
            if rating_val is not None:
                try:
                    rating_val_str = f"{float(rating_val):.1f}"
                except Exception:
                    rating_val_str = "-"

            image_url = final_pick.get("image_url") or (cached_place_image_url(kakao_place_url) if kakao_place_url else None)

            st.markdown(
                f"""
                <div class="final">
                  <div class="final-title">오늘의 최종 한 곳</div>
                  <div class="final-name">⭐ {html.escape(final_name)}</div>
                  <div class="muted" style="margin-top:6px">
                    거리 <b>{final_d_str}</b> · 별점 <b>{rating_val_str}</b>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            c_left, c_right = st.columns([1.15, 0.85])
            with c_left:
                st.write("")
                st.markdown(
                    f"- **주소**: `{final_addr or '-'}`\n"
                    f"- **지도**: {final_url if final_url else '-'}\n"
                    f"- **카카오 장소**: {kakao_place_url if kakao_place_url else '-'}"
                )
            with c_right:
                if image_url:
                    try:
                        st.image(image_url, caption="가게 대표 이미지", use_container_width=True)
                    except Exception:
                        st.info("이미지를 불러오지 못했어요.")
                else:
                    st.info("표시할 이미지가 아직 없어요.")

        user_lat = st.session_state.get("last_user_lat")
        user_lng = st.session_state.get("last_user_lng")
        if user_lat is not None and user_lng is not None:
            st.markdown("<div class='section-title'>지도로 보기</div>", unsafe_allow_html=True)
            candidates_for_map = st.session_state.get("last_candidates") or []
            _render_map(
                user_lat=float(user_lat),
                user_lng=float(user_lng),
                candidates=candidates_for_map,
                final_pick=final_pick,
                radius_m=int(radius_m),
            )

        # 카드를 2열 레이아웃으로 배치
        if bool(roulette_on) and picks:
            # 룰렛 애니메이션: 실제 최종 픽이 아니라 후보 풀에서 짧게 스핀
            pool = st.session_state.get("last_candidates") or []
            if bool(rating_on) and st.session_state.get("last_used_rating_filter"):
                pool = st.session_state.get("last_picks") or pool
            if pool:
                enriched: list[tuple[float, dict[str, Any]]] = []
                for c in pool:
                    d = _get_distance_m(c)
                    enriched.append((d if d is not None else 1e18, c))
                enriched.sort(key=lambda x: x[0])

                top_percent_local = max(10.0, min(100.0, float(top_percent)))
                top_size_local = max(1, int(math.ceil(len(enriched) * top_percent_local / 100.0)))
                roulette_pool = [c for _d, c in enriched[:top_size_local]] or pool

                rng = random.Random(_stable_seed(date.today().isoformat(), int(st.session_state["round_no"]), params_signature_str, "roulette"))

                def _roulette_pick() -> dict[str, Any]:
                    if not roulette_pool:
                        return {}
                    if not bool(weighted_by_distance):
                        return roulette_pool[rng.randrange(0, len(roulette_pool))]

                    weights: list[float] = []
                    for it in roulette_pool:
                        d = _get_distance_m(it)
                        if d is None or d >= 1e17:
                            weights.append(1.0)
                        else:
                            weights.append(1.0 / (d + 1.0))

                    total_w = sum(weights)
                    if total_w <= 0:
                        return roulette_pool[rng.randrange(0, len(roulette_pool))]

                    r = rng.random() * total_w
                    acc = 0.0
                    for it, w in zip(roulette_pool, weights):
                        acc += w
                        if r <= acc:
                            return it
                    return roulette_pool[-1]

                roulette_placeholder = st.empty()
                progress = st.progress(0)
                for spin in range(int(roulette_rounds)):
                    c = _roulette_pick()
                    title = str(c.get("name") or "Unknown")
                    d = _get_distance_m(c)
                    d_str = f"{int(d)}m" if d is not None else "-"
                    roulette_placeholder.markdown(
                        f"### 룰렛 스핀 {spin + 1}/{roulette_rounds}\n- {title}\n- 거리: {d_str}"
                    )
                    progress.progress(int((spin + 1) / roulette_rounds * 100))
                    time.sleep(0.06)
                progress.progress(100)

        st.markdown("<div class='section-title'>후보 리스트</div>", unsafe_allow_html=True)
        cols = st.columns(2, gap="large")
        for i, c in enumerate(picks):
            with cols[i % 2]:
                # 별점 표시를 위해, 렌더 직전에 최종 picks는 별점/이미지를 보강합니다.
                if bool(rating_on) and c.get("rating") is None:
                    kakao_place_url = _to_kakao_place_url(c)
                    if kakao_place_url:
                        img_u, r_v = cached_kakao_place_meta(kakao_place_url)
                        if img_u and not c.get("image_url"):
                            c["image_url"] = img_u
                        if r_v is not None:
                            c["rating"] = r_v
                _render_pick_card(c, i + 1, show_distance=bool(show_distance), show_address=bool(show_address))
                st.write("")

        distances = [p.get("distance_m") for p in picks if p.get("distance_m") is not None]
        best_distance = min([float(x) for x in distances], default=None) if distances else None
        if best_distance is not None:
            if best_distance <= 500:
                st.success("오점뭐 초근거리 배지 획득!")
                st.balloons()
            elif best_distance <= 1000:
                st.info("오점뭐 근거리 배지 획득!")
            else:
                st.write("오점뭐 일반 배지. 오늘은 무난하게!")

    st.caption("오점뭐 · Kakao Local 기반 후보 + (선택) 별점 스크래핑. GPS는 브라우저 권한이 필요합니다.")


if __name__ == "__main__":
    main()

