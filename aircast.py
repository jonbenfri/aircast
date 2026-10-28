#!/usr/bin/env python3
"""aircast: air quality forecasting. Forecasts when US AQI near you will exceed a threshold and
pushes alerts to your phone through an ntfy server.

Data:
  * Open-Meteo air-quality API (CAMS model): hourly US AQI, past + forecast.
    Free, no key.
  * AirNow (optional, free key): real monitor observations, used to correct
    the model's bias at the current hour.

Math:
  corrected(h) = model(h) + bias * exp(-h / tau)       bias decays with lead time
  sigma(h)     = sqrt(sigma0^2 + (growth * h)^2)        uncertainty grows with lead
  P_exceed(h)  = 1 - Phi((threshold - corrected(h)) / sigma(h))

  bias is an exponentially weighted average of recent (observed - model);
  sigma0 is learned from the spread of those residuals once enough exist.

Wind (Open-Meteo weather forecast, current + hourly 10 m wind):
  The model AQI is also sampled on a ring of points around you. For each
  forecast hour we look at the ring point the wind is blowing FROM, and the
  time air needs to travel from there (ring_km / wind speed), giving an
  "advected" AQI: what upwind air should bring in. Then
    corrected(h) += w(speed) * (advected(h) - corrected(h))
    sigma(h)      = hypot(sigma(h), 0.5 * |advected - local|, stagnation)
  w is 0 in calm air (direction is meaningless) and ramps up to wind_weight
  by ~3 m/s. Calm air (< 2 m/s) adds a stagnation term to sigma because
  pollution tends to accumulate locally.

Transport (forecast winds, hour by hour, no smoothing):
  Air arriving at hour h is traced backwards through the hourly forecast
  winds (a back-trajectory), so spikes and wind shifts count where they
  happen instead of being averaged away. Transport wind is the vector mean
  of the 10 m and 850 hPa winds (a rough mixed-layer wind). Five scenarios
  are traced: the forecast as-is, direction +/-20 deg, speed x0.7 and x1.3.
  Where they disagree, uncertainty goes up.

Upwind monitors (AirNow, needs key): models often miss wildfire smoke. For
  each hour h, monitors near each scenario's starting point say what air
  is on its way, assuming the plume persists:
    corrected(h) += obs_weight * agreement * (upwind_obs(h) - corrected(h))

Smoke (NOAA HMS satellite smoke polygons, no key): a separate state:
  0 none within 100 mi, 1 within 100 mi, 2 within 50 mi, 3 overhead,
  4 dense overhead (overlapping plumes or medium/heavy density), plus an
  "incoming" flag when the back-trajectories start inside a plume. AQI
  monitors don't measure smoke VOCs, so this is tracked separately.

Stdlib only (Python 3.8+). Configured through environment variables; see
deploy/aircast.env.example.
"""
from __future__ import annotations

import argparse
import calendar
import io
import json
import logging
import math
import os
import struct
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("aircast")

OPEN_METEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
OPEN_METEO_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
# The old /aq/observation/latLong/current/ was retired 2026-09-30 (HTTP 410).
AIRNOW_URL = "https://www.airnowapi.org/aq/observation/current/ziplatlong/"
AIRNOW_DATA_URL = "https://www.airnowapi.org/aq/data/"
# NOAA Hazard Mapping System smoke polygons, one zip per UTC day (strftime patterns, tried in order).
HMS_URLS = [
    "https://satepsanone.nesdis.noaa.gov/pub/FIRE/web/HMS/Smoke_Polygons/Shapefile/%Y/%m/hms_smoke%Y%m%d.zip",
    "https://satepsanone.nesdis.noaa.gov/pub/FIRE/web/HMS/Smoke_Polygons/Shapefile/%Y/hms_smoke%Y%m%d.zip",
]
MPH = 2.23694  # m/s -> mph
MI = 1.609344  # km per mile
HOUR = 3600


# --------------------------------------------------------------------------- config

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Config:
    lat: float
    lon: float
    threshold: float = 35.0
    horizon_hours: int = 12
    poll_good_minutes: float = 60.0
    poll_bad_minutes: float = 30.0
    alert_prob: float = 0.5
    clear_prob: float = 0.2
    hysteresis: float = 5.0
    remind_hours: float = 6.0
    tau_hours: float = 12.0
    sigma0: float = 8.0
    sigma_growth: float = 0.6
    fail_alert_after: int = 6
    use_wind: bool = True
    ring_km: float = 50.0
    wind_weight: float = 0.35
    stagnant_ms: float = 2.0
    stagnant_sigma: float = 5.0
    upwind_km: float = 250.0
    watch_aqi: Optional[float] = None
    watch_hours: int = 24
    transport: str = "mixed"
    smoke: bool = True
    hms_url: Optional[str] = None
    smoke_clear_hours: float = 3.0
    smoke_far_mi: float = 200.0
    dilution_km: float = 400.0
    learn: bool = True
    learn_shrink: float = 50.0
    log_days: int = 60
    obs_weight: float = 0.6
    airnow_key: Optional[str] = None
    airnow_distance: int = 25
    ntfy_url: Optional[str] = None
    ntfy_topic: Optional[str] = None
    ntfy_token: Optional[str] = None
    ntfy_icon: Optional[str] = None
    place: Optional[str] = None
    db_path: str = "aircast.db"

    @classmethod
    def from_env(cls) -> "Config":
        lat, lon = _env("AQI_LAT"), _env("AQI_LON")
        if lat is None or lon is None:
            sys.exit("AQI_LAT and AQI_LON must be set")
        f = lambda n, d: float(_env(n, str(d)))  # noqa: E731
        return cls(
            lat=float(lat),
            lon=float(lon),
            threshold=f("AQI_THRESHOLD", 35),
            horizon_hours=int(f("AQI_HORIZON_HOURS", 12)),
            poll_good_minutes=f("AQI_POLL_GOOD_MINUTES", 60),
            poll_bad_minutes=f("AQI_POLL_BAD_MINUTES", 30),
            alert_prob=f("AQI_ALERT_PROB", 0.5),
            clear_prob=f("AQI_CLEAR_PROB", 0.2),
            hysteresis=f("AQI_HYSTERESIS", 5),
            remind_hours=f("AQI_REMIND_HOURS", 6),
            tau_hours=f("AQI_TAU_HOURS", 12),
            sigma0=f("AQI_SIGMA0", 8),
            sigma_growth=f("AQI_SIGMA_GROWTH", 0.6),
            fail_alert_after=int(f("AQI_FAIL_ALERT_AFTER", 6)),
            use_wind=_env("AQI_USE_WIND", "1") not in ("0", "false", "no"),
            ring_km=f("AQI_RING_KM", 50),
            wind_weight=f("AQI_WIND_WEIGHT", 0.35),
            stagnant_ms=f("AQI_STAGNANT_MS", 2.0),
            stagnant_sigma=f("AQI_STAGNANT_SIGMA", 5),
            upwind_km=f("AQI_UPWIND_KM", 250),
            watch_aqi=float(_env("AQI_WATCH_AQI")) if _env("AQI_WATCH_AQI") else None,
            watch_hours=int(f("AQI_WATCH_HOURS", 24)),
            transport=_env("AQI_TRANSPORT", "mixed"),
            smoke=_env("AQI_SMOKE", "1") not in ("0", "false", "no"),
            hms_url=_env("AQI_HMS_URL"),
            smoke_clear_hours=f("AQI_SMOKE_CLEAR_HOURS", 3),
            smoke_far_mi=f("AQI_SMOKE_FAR_MI", 200),
            dilution_km=f("AQI_DILUTION_KM", 400),
            learn=_env("AQI_LEARN", "1") not in ("0", "false", "no"),
            learn_shrink=f("AQI_LEARN_SHRINK", 50),
            log_days=int(f("AQI_LOG_DAYS", 60)),
            obs_weight=f("AQI_OBS_WEIGHT", 0.6),
            airnow_key=_env("AIRNOW_API_KEY"),
            airnow_distance=int(f("AIRNOW_DISTANCE_MILES", 25)),
            ntfy_url=_env("NTFY_URL"),
            ntfy_topic=_env("NTFY_TOPIC"),
            ntfy_token=_env("NTFY_TOKEN"),
            ntfy_icon=_env("NTFY_ICON"),
            place=_env("AQI_PLACE"),
            db_path=_env("AQI_DB", "aircast.db"),
        )


# --------------------------------------------------------------------------- data

def _get_json(url: str, params: Dict[str, object], timeout: float = 20):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "aircast/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _parse_series(block: dict, var: str) -> List[Tuple[int, float]]:
    return [(int(t), float(v)) for t, v in zip(block["time"], block[var]) if v is not None]


def fetch_model(cfg: Config) -> List[Tuple[int, float]]:
    """Hourly (unix_ts, us_aqi) from Open-Meteo, past 24h through +3 days."""
    data = _get_json(OPEN_METEO_URL, {
        "latitude": cfg.lat,
        "longitude": cfg.lon,
        "hourly": "us_aqi",
        "past_days": 1,
        "forecast_days": 3,
        "timeformat": "unixtime",
    })
    return _parse_series(data["hourly"], "us_aqi")


RING_BEARINGS = [0, 45, 90, 135, 180, 225, 270, 315]


def destination(lat: float, lon: float, bearing_deg: float, km: float) -> Tuple[float, float]:
    """Point `km` away on a great circle along `bearing_deg` (0 = north)."""
    r = 6371.0
    p1, l1, b, d = math.radians(lat), math.radians(lon), math.radians(bearing_deg), km / r
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def fetch_ring(cfg: Config, km: float) -> Dict[int, List[Tuple[int, float]]]:
    """Model AQI at 8 points `km` around you, keyed by bearing. One request."""
    pts = [destination(cfg.lat, cfg.lon, b, km) for b in RING_BEARINGS]
    data = _get_json(OPEN_METEO_URL, {
        "latitude": ",".join(f"{la:.4f}" for la, _ in pts),
        "longitude": ",".join(f"{lo:.4f}" for _, lo in pts),
        "hourly": "us_aqi",
        "past_days": 1,
        "forecast_days": 3,
        "timeformat": "unixtime",
    })
    if isinstance(data, dict):  # multi-location responses are a list
        data = [data]
    return {b: _parse_series(d["hourly"], "us_aqi") for b, d in zip(RING_BEARINGS, data)}


@dataclass
class Wind:
    speed: float      # m/s
    direction: float  # degrees the wind blows FROM (meteorological)
    gust: Optional[float] = None


def _winds(h: dict, speed: str, direction: str, gust: Optional[str] = None) -> Dict[int, Wind]:
    if speed not in h or direction not in h:
        return {}
    gusts = h.get(gust) if gust else None
    out = {}
    for i, (t, sp, d) in enumerate(zip(h["time"], h[speed], h[direction])):
        if sp is not None and d is not None:
            g = gusts[i] if gusts and gusts[i] is not None else None
            out[int(t)] = Wind(float(sp), float(d), g)
    return out


@dataclass
class WindData:
    current: Optional[Wind]
    surface: Dict[int, Wind]    # 10 m: what you feel, used for messages and stagnation
    transport: Dict[int, Wind]  # mean of 10 m and 850 hPa (~5,000 ft): carries air near the ground
    aloft: Dict[int, Wind]      # 700 hPa (~10,000 ft): carries smoke plumes
    blh: Dict[int, float]       # boundary layer (mixing) height in metres


def fetch_wind(cfg: Config) -> WindData:
    """Hourly winds at three heights plus mixing height, keyed by unix ts."""
    surface_vars = ["wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"]
    upper_vars = ["wind_speed_850hPa", "wind_direction_850hPa", "wind_speed_700hPa", "wind_direction_700hPa"]
    params = {
        "latitude": cfg.lat,
        "longitude": cfg.lon,
        "current": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms",
        "past_days": 1,
        "forecast_days": 3,
        "timeformat": "unixtime",
    }
    hourly = surface_vars + (upper_vars if cfg.transport == "mixed" else [])
    try:
        data = _get_json(OPEN_METEO_WEATHER_URL, dict(params, hourly=",".join(hourly)))
    except urllib.error.HTTPError as e:
        if cfg.transport != "mixed":
            raise
        log.warning("upper-level wind unavailable (%s); using 10 m wind for transport", e)
        data = _get_json(OPEN_METEO_WEATHER_URL, dict(params, hourly=",".join(surface_vars)))
    h = data["hourly"]
    surface = _winds(h, "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m")
    w850 = _winds(h, "wind_speed_850hPa", "wind_direction_850hPa")
    w700 = _winds(h, "wind_speed_700hPa", "wind_direction_700hPa")
    transport = {t: (mean_wind([w, w850[t]]) if t in w850 else w) for t, w in surface.items()}
    c = data.get("current") or {}
    cur = None
    if c.get("wind_speed_10m") is not None and c.get("wind_direction_10m") is not None:
        cur = Wind(float(c["wind_speed_10m"]), float(c["wind_direction_10m"]))

    blh: Dict[int, float] = {}
    try:  # separate request: not every model behind the API provides it
        b = _get_json(OPEN_METEO_WEATHER_URL, dict(params, hourly="boundary_layer_height"))["hourly"]
        blh = {int(t): float(v) for t, v in zip(b["time"], b.get("boundary_layer_height", [])) if v is not None}
    except Exception as e:
        log.warning("mixing height unavailable: %s", e)
    return WindData(cur, surface, transport, w700 or transport, blh)


def blh_at(blh: Dict[int, float], ts: float) -> Optional[float]:
    if not blh:
        return None
    t = min(blh, key=lambda k: abs(k - ts))
    return blh[t] if abs(t - ts) <= 2 * HOUR else None


def mixing_text(m: Optional[float]) -> str:
    """Whether smoke aloft is likely to mix down to the ground."""
    if m is None:
        return ""
    ft = round(m * 3.281 / 100) * 100
    if m >= 1500:
        return f"Mixing: deep ~{ft:,} ft{SEP}smoke aloft likely reaching the ground"
    if m >= 500:
        return f"Mixing: moderate ~{ft:,} ft{SEP}some smoke aloft may reach the ground"
    return f"Mixing: shallow ~{ft:,} ft{SEP}smoke aloft likely stays up; local pollution trapped low"


def fetch_points(cfg: Config, pts: List[Tuple[float, float]]) -> List[List[Tuple[int, float]]]:
    """Model AQI series at arbitrary points. One request."""
    data = _get_json(OPEN_METEO_URL, {
        "latitude": ",".join(f"{la:.4f}" for la, _ in pts),
        "longitude": ",".join(f"{lo:.4f}" for _, lo in pts),
        "hourly": "us_aqi",
        "past_days": 1,
        "forecast_days": 1,
        "timeformat": "unixtime",
    })
    if isinstance(data, dict):
        data = [data]
    return [_parse_series(d["hourly"], "us_aqi") for d in data]


def fetch_observation(cfg: Config) -> Optional[Tuple[int, float, str]]:
    """Current observed overall AQI (max over pollutants) for your reporting area, or None."""
    if not cfg.airnow_key:
        return None
    rows = _get_json(AIRNOW_URL, {
        "format": "application/json",
        "latitude": cfg.lat,
        "longitude": cfg.lon,
        "distance": cfg.airnow_distance,
        "API_KEY": cfg.airnow_key,
    })
    rows = [r for r in rows if isinstance(r.get("AQI"), (int, float)) and r["AQI"] >= 0]
    if not rows:
        return None
    worst = max(rows, key=lambda r: r["AQI"])
    # This endpoint reports local observation hours without a usable offset; the reading is
    # current to within ~1h, so stamp it with the start of this hour.
    ts = int(time.time()) // HOUR * HOUR
    area = ", ".join(x for x in (worst.get("ReportingArea"), worst.get("StateCode")) if x)
    return ts, float(worst["AQI"]), area or "AirNow"


@dataclass
class Station:
    lat: float
    lon: float
    aqi: float
    name: str
    ts: Optional[int] = None  # start of the measured hour (UTC epoch)


def _utc_hour(v: str) -> Optional[int]:
    try:
        return calendar.timegm(time.strptime(v[:13], "%Y-%m-%dT%H"))
    except (ValueError, TypeError):
        return None


def nearest_observation(cfg: Config, stations: List["Station"]) -> Optional[Tuple[int, float, str]]:
    """Fallback current reading: the nearest monitor within airnow_distance miles."""
    best = None
    for st in stations:
        km, _ = distance_bearing(cfg.lat, cfg.lon, st.lat, st.lon)
        if km > cfg.airnow_distance * 1.609:
            continue
        # Near-road monitors measure highway exhaust, not neighbourhood air: rank them last.
        rank = ("near" in st.name.lower() and "road" in st.name.lower(), km)
        if best is None or rank < best[0]:
            best = (rank, st)
    if best is None:
        return None
    (_, km), st = best
    return st.ts or int(time.time()) // HOUR * HOUR, st.aqi, f"{st.name}, {km / 1.609:.0f} mi"


def fetch_stations(cfg: Config) -> List[Station]:
    """Latest hourly AQI (worst pollutant) at every AirNow monitor within upwind_km.

    AirNow only takes a bounding box, so the box corners are trimmed back to a circle.

    Includes ozone/CO/NO2, not just PM: smoke VOCs + NOx make ozone downwind.
    """
    if not cfg.airnow_key:
        return []
    dlat = cfg.upwind_km / 111.0
    dlon = dlat / max(0.2, math.cos(math.radians(cfg.lat)))
    t = time.gmtime(time.time() - 2 * HOUR)  # data lags ~1h; take the last couple of hours
    start = time.strftime("%Y-%m-%dT%H", t)
    end = time.strftime("%Y-%m-%dT%H", time.gmtime())
    rows = _get_json(AIRNOW_DATA_URL, {
        "startDate": start,
        "endDate": end,
        "parameters": "OZONE,PM25,PM10,CO,NO2",
        "BBOX": f"{cfg.lon - dlon:.4f},{cfg.lat - dlat:.4f},{cfg.lon + dlon:.4f},{cfg.lat + dlat:.4f}",
        "dataType": "A",
        "format": "application/json",
        "verbose": 1,
        "API_KEY": cfg.airnow_key,
    })
    latest: Dict[Tuple[float, float], Tuple[str, Station]] = {}
    for r in rows:
        aqi = r.get("AQI")
        if not isinstance(aqi, (int, float)) or aqi < 0:
            continue
        key = (round(float(r["Latitude"]), 4), round(float(r["Longitude"]), 4))
        utc = str(r.get("UTC", ""))
        st = Station(key[0], key[1], float(aqi), str(r.get("SiteName") or r.get("Parameter") or "?"),
                     _utc_hour(utc))
        prev = latest.get(key)
        # Newest hour wins; within the same hour keep the worse pollutant.
        if prev is None or utc > prev[0] or (utc == prev[0] and st.aqi > prev[1].aqi):
            latest[key] = (utc, st)
    return [st for _, st in latest.values()
            if distance_bearing(cfg.lat, cfg.lon, st.lat, st.lon)[0] <= cfg.upwind_km]


# --------------------------------------------------------------------------- math

def interp(series: List[Tuple[int, float]], ts: float) -> Optional[float]:
    """Linear interpolation in a time-sorted series; None outside its range."""
    if not series or ts < series[0][0] or ts > series[-1][0]:
        return None
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t0 <= ts <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (ts - t0) / (t1 - t0)
    return series[-1][1]


def estimate_bias(residuals: List[Tuple[int, float]], now: float, half_life_h: float = 3.0) -> float:
    """Time-weighted average of (observed - model); recent hours count most."""
    num = den = 0.0
    for ts, r in residuals:
        w = 0.5 ** (max(0.0, now - ts) / HOUR / half_life_h)
        num += w * r
        den += w
    return num / den if den else 0.0


def estimate_sigma0(residuals: List[Tuple[int, float]], default: float, min_n: int = 24) -> float:
    """Spread of model error at lead 0, learned once there is enough history."""
    if len(residuals) < min_n:
        return default
    vals = [r for _, r in residuals]
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
    return max(3.0, sd)


COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass(deg: float) -> str:
    return COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


def ring_value(ring: Dict[int, List[Tuple[int, float]]], bearing: float, ts: float) -> Optional[float]:
    """Model AQI on the ring at `bearing`, interpolated between the two nearest ring points."""
    bearing %= 360
    step = 360 / len(RING_BEARINGS)
    i = int(bearing // step)
    b0, b1 = RING_BEARINGS[i], RING_BEARINGS[(i + 1) % len(RING_BEARINGS)]
    v0, v1 = interp(ring.get(b0, []), ts), interp(ring.get(b1, []), ts)
    if v0 is None or v1 is None:
        return v0 if v1 is None else v1
    frac = (bearing - b0) / step
    return v0 + (v1 - v0) * frac


def advected(ring: Dict[int, List[Tuple[int, float]]], wind: Wind, ring_km: float,
             arrival_ts: float, max_travel_h: float = 12.0) -> Optional[Tuple[float, float]]:
    """(AQI that upwind air should bring at arrival_ts, travel hours), or None if calm."""
    if wind.speed < 0.5:
        return None
    travel_h = ring_km / (wind.speed * 3.6)
    if travel_h > max_travel_h:
        return None
    v = ring_value(ring, wind.direction, arrival_ts - travel_h * HOUR)
    return None if v is None else (v, travel_h)


def distance_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    """Great-circle (km, initial bearing degrees) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * 6371.0 * math.asin(math.sqrt(a))
    brg = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                  math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return km, brg % 360


def mean_wind(winds: List[Wind]) -> Optional[Wind]:
    """Vector average (a north wind and a south wind cancel, as air would)."""
    if not winds:
        return None
    u = sum(-w.speed * math.sin(math.radians(w.direction)) for w in winds) / len(winds)
    v = sum(-w.speed * math.cos(math.radians(w.direction)) for w in winds) / len(winds)
    return Wind(math.hypot(u, v), math.degrees(math.atan2(-u, -v)) % 360)


# US AQI for PM2.5 (EPA, 2024 breakpoints), as a continuous piecewise-linear curve.
PM_AQI = [(0.0, 0), (9.0, 50), (35.4, 100), (55.4, 150), (125.4, 200), (225.4, 300), (325.4, 500)]


def _pw(x: float, pts: List[Tuple[float, float]]) -> float:
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (max(x, x0) - x0) / (x1 - x0)
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    return y1 + (y1 - y0) * (x - x1) / (x1 - x0)


def aqi_to_pm(aqi: float) -> float:
    """AQI -> PM2.5-equivalent ug/m3, so air can be mixed and diluted in real units."""
    return _pw(max(0.0, aqi), [(a, c) for c, a in PM_AQI])


def pm_to_aqi(pm: float) -> float:
    return _pw(max(0.0, pm), PM_AQI)


def dilute(cfg: Config, upwind_aqi: float, background_aqi: float, path_km: float) -> float:
    """AQI expected when upwind air arrives after travelling path_km and mixing with local air.

    The excess over background decays exponentially with distance (e-folding length dilution_km).
    """
    bg = aqi_to_pm(background_aqi)
    f = math.exp(-max(0.0, path_km) / cfg.dilution_km)
    return pm_to_aqi(bg + (aqi_to_pm(upwind_aqi) - bg) * f)


def local_km(lat0: float, lon0: float, lat: float, lon: float) -> Tuple[float, float]:
    """(east, north) km of a point relative to (lat0, lon0); fine within a few hundred km."""
    return ((lon - lon0) * 111.32 * math.cos(math.radians(lat0)), (lat - lat0) * 110.57)


def offset_latlon(lat: float, lon: float, east_km: float, north_km: float) -> Tuple[float, float]:
    return lat + north_km / 110.57, lon + east_km / (111.32 * math.cos(math.radians(lat)))


def _uv(w: Wind) -> Tuple[float, float]:
    """Air motion (east, north) in m/s; direction is where the wind comes FROM."""
    r = math.radians(w.direction)
    return -w.speed * math.sin(r), -w.speed * math.cos(r)


# Wind scenarios: (direction offset deg, speed factor). Member 0 is the forecast as-is.
SCENARIOS = [(0.0, 1.0), (-20.0, 1.0), (20.0, 1.0), (0.0, 0.7), (0.0, 1.3)]


@dataclass
class Trajectory:
    """Where the air arriving at `ts` is right now, under each wind scenario."""
    ts: int
    lead_h: float
    origins: List[Tuple[float, float, float]]  # (lat, lon, path_km) per scenario

    @property
    def path_km(self) -> float:
        return self.origins[0][2]


def back_trajectories(cfg: Config, now: float, transport: Dict[int, Wind],
                      hours: float, step_s: float = 600) -> List[Trajectory]:
    """Trace each arriving hour back to `now` through the hourly winds (uniform-field approximation)."""
    series = sorted((t, *_uv(w)) for t, w in transport.items())
    if len(series) < 2:
        return []
    times = [t for t, _, _ in series]

    def uv_at(t: float) -> Tuple[float, float]:
        if t <= times[0]:
            return series[0][1], series[0][2]
        if t >= times[-1]:
            return series[-1][1], series[-1][2]
        i = max(k for k in range(len(times)) if times[k] <= t)
        (t0, u0, v0), (t1, u1, v1) = series[i], series[i + 1]
        f = (t - t0) / (t1 - t0)
        return u0 + (u1 - u0) * f, v0 + (v1 - v0) * f

    out = []
    for ts in times:
        if ts <= now or ts > now + hours * HOUR:
            continue
        origins = []
        for rot, fac in SCENARIOS:
            c, s_ = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            e = n = path = 0.0
            t = float(ts)
            while t > now:
                dt = min(step_s, t - now)
                u, v = uv_at(t - dt / 2)
                u, v = (u * c + v * s_) * fac, (v * c - u * s_) * fac
                e -= u * dt / 1000
                n -= v * dt / 1000
                path += math.hypot(u, v) * dt / 1000
                t -= dt
            origins.append((*offset_latlon(cfg.lat, cfg.lon, e, n), path))
        out.append(Trajectory(ts, (ts - now) / HOUR, origins))
    return out


def traj_obs(stations: List[Station], tr: Trajectory) -> Optional[Tuple[float, float, float]]:
    """(PM2.5-equivalent ug/m3 now at the origins, spread across scenarios, agreement 0..1),
    from monitors near the trajectory origins. Averaged in concentration, not AQI."""
    ests, support = [], 0.0
    for la, lo, path in tr.origins:
        sig = 10 + 0.15 * path  # starting-point uncertainty grows with distance travelled
        num = den = 0.0
        for st in stations:
            e, n = local_km(la, lo, st.lat, st.lon)
            d = math.hypot(e, n)
            if d < 3 * sig:
                w = math.exp(-0.5 * (d / sig) ** 2)
                num += w * aqi_to_pm(st.aqi)
                den += w
        if den > 0.05:
            ests.append(num / den)
            support += min(1.0, den)
    if not ests:
        return None
    mean = sum(ests) / len(ests)
    spread = math.sqrt(sum((x - mean) ** 2 for x in ests) / len(ests))
    return mean, spread, support / len(tr.origins)


@dataclass
class Threat:
    """Bad air that forecast winds are expected to carry to you."""
    aqi: float
    name: str
    km: float
    bearing: float
    eta: int
    support: float  # fraction of wind scenarios that bring it
    obs_ts: Optional[int] = None  # when the source was measured (None for the model)
    arrive_aqi: Optional[float] = None  # expected AQI here after dilution on the way


def find_threats(cfg: Config, stations: List[Station], trajs: List[Trajectory],
                 origin_model: Optional[Dict[int, float]] = None,
                 background: Optional[float] = None) -> List[Threat]:
    """Monitors (or model points) above watch_aqi that the back-trajectories start near, worst first."""
    limit = cfg.threshold if cfg.watch_aqi is None else cfg.watch_aqi
    hits: Dict[str, Dict] = {}

    def hit(key, aqi, name, lat, lon, ts, member, path, obs_ts=None):
        km, brg = distance_bearing(cfg.lat, cfg.lon, lat, lon)
        h = hits.setdefault(key, {"aqi": aqi, "name": name, "km": km, "brg": brg, "obs_ts": obs_ts,
                                  "eta": None, "eta_any": ts, "members": set(), "arrive": None})
        if background is not None:
            arr = dilute(cfg, aqi, background, path)
            h["arrive"] = arr if h["arrive"] is None else max(h["arrive"], arr)
        h["members"].add(member)
        h["eta_any"] = min(h["eta_any"], ts)
        if member == 0 and (h["eta"] is None or ts < h["eta"]):
            h["eta"] = ts

    for tr in trajs:
        for i, (la, lo, path) in enumerate(tr.origins):
            if path < 10:  # air that hasn't moved is "here", covered by the current reading
                continue
            r = 15 + 0.15 * path
            for st in stations:
                if st.aqi > limit and math.hypot(*local_km(la, lo, st.lat, st.lon)) <= r:
                    hit(f"{st.lat:.3f},{st.lon:.3f}", st.aqi, st.name, st.lat, st.lon, tr.ts, i, path, st.ts)
        v = (origin_model or {}).get(tr.ts)
        la, lo, path = tr.origins[0]
        if v is not None and v > limit and path >= 30:
            _, brg = distance_bearing(cfg.lat, cfg.lon, la, lo)
            hit(f"model@{compass(brg)}", v, "model", la, lo, tr.ts, 0, path)

    out = [Threat(h["aqi"], h["name"], h["km"], h["brg"], h["eta"] or h["eta_any"],
                  len(h["members"]) / len(SCENARIOS), h["obs_ts"], h["arrive"]) for h in hits.values()]
    return sorted(out, key=lambda t: -t.aqi * t.support)


def wind_weight(speed: float, max_weight: float) -> float:
    """0 when calm (direction unreliable), ramping to max_weight at 3 m/s."""
    return max_weight * min(1.0, max(0.0, (speed - 0.5) / 2.5))


def prob_exceed(mean: float, sigma: float, threshold: float) -> float:
    z = (threshold - mean) / sigma
    return 0.5 * math.erfc(z / math.sqrt(2))


@dataclass
class HourForecast:
    ts: int
    lead_h: float
    model: float
    corrected: float
    sigma: float
    p_exceed: float
    wind: Optional[Wind] = None
    upwind: Optional[float] = None
    traj: Optional[Trajectory] = None
    base: Optional[float] = None    # forecast before the learned correction (what gets learned from)
    blh: Optional[float] = None     # mixing height, m


@dataclass
class Assessment:
    now: int
    current: float
    current_source: str
    bias: float
    sigma0: float
    wind_now: Optional[Wind] = None
    upwind_now: Optional[float] = None
    hours: List[HourForecast] = field(default_factory=list)
    threats: List[Threat] = field(default_factory=list)
    surface: Dict[int, Wind] = field(default_factory=dict)
    trajs: List[Trajectory] = field(default_factory=list)
    smoke: Optional["Smoke"] = None
    current_ts: Optional[int] = None  # when the current reading was measured; None = model estimate
    place: str = ""
    blh: Dict[int, float] = field(default_factory=dict)
    learned: Dict[int, Tuple[int, float, float]] = field(default_factory=dict)  # bucket -> (n, bias, rmse)

    @property
    def max_p(self) -> float:
        return max((h.p_exceed for h in self.hours), default=0.0)

    @property
    def peak(self) -> Optional[HourForecast]:
        return max(self.hours, key=lambda h: h.corrected, default=None)

    def first_exceed(self, p: float) -> Optional[HourForecast]:
        return next((h for h in self.hours if h.p_exceed >= p), None)


def assess(cfg: Config, model: List[Tuple[int, float]], residuals: List[Tuple[int, float]],
           observation: Optional[Tuple[int, float, str]], now: float,
           wind_now: Optional[Wind] = None, wind_hourly: Optional[Dict[int, Wind]] = None,
           ring: Optional[Dict[int, List[Tuple[int, float]]]] = None,
           stations: Optional[List[Station]] = None,
           trajs: Optional[List[Trajectory]] = None,
           origin_model: Optional[Dict[int, float]] = None,
           plumes: Optional[List["Plume"]] = None,
           trajs_aloft: Optional[List[Trajectory]] = None,
           blh: Optional[Dict[int, float]] = None,
           learned: Optional[Dict[int, Tuple[int, float, float]]] = None) -> Assessment:
    """wind_hourly is the 10 m wind; trajs default to tracing it if not given."""
    bias = estimate_bias(residuals, now)
    sigma0 = estimate_sigma0(residuals, cfg.sigma0)
    model_now = interp(model, now)
    if model_now is None:
        raise ValueError("model series does not cover the current time")
    if observation is not None:
        current, source, current_ts = observation[1], f"AirNow, {observation[2]}", observation[0]
    else:
        current, source, current_ts = model_now + bias, "model", None

    if trajs is None:
        trajs = back_trajectories(cfg, now, wind_hourly or {}, max(cfg.watch_hours, cfg.horizon_hours))
    by_ts = {tr.ts: tr for tr in trajs}
    a = Assessment(now=int(now), current=current, current_source=source, bias=bias,
                   sigma0=sigma0, wind_now=wind_now, surface=wind_hourly or {}, trajs=trajs,
                   current_ts=current_ts,
                   place=cfg.place or (observation[2].split(",")[0] if observation else ""),
                   blh=blh or {}, learned=learned or {})
    a.threats = find_threats(cfg, stations or [], [t for t in trajs if t.lead_h <= cfg.watch_hours],
                             origin_model, current)
    if plumes is not None:
        a.smoke = assess_smoke(cfg, plumes, trajs, trajs_aloft, a.blh, now)
    if wind_now and ring:
        adv = advected(ring, wind_now, cfg.ring_km, now)
        a.upwind_now = adv[0] if adv else None
    if stations:
        # Worst air arriving within ~3h: what "if this persists" brings.
        ests = [traj_obs(stations, tr) for tr in trajs if tr.lead_h <= 3 and tr.path_km >= 10]
        vals = [pm_to_aqi(e[0]) for e in ests if e]
        if vals:
            a.upwind_now = max(vals + ([a.upwind_now] if a.upwind_now is not None else []))

    end = now + cfg.horizon_hours * HOUR
    for ts, m in model:
        if ts <= now or ts > end:
            continue
        lead = (ts - now) / HOUR
        corrected = max(0.0, m + bias * math.exp(-lead / cfg.tau_hours))
        sigma = math.hypot(sigma0, cfg.sigma_growth * lead)

        # Use the current wind for the first hour, forecast wind after that.
        w = wind_now if (lead <= 1 and wind_now) else (wind_hourly or {}).get(ts)
        up = None
        if w is not None:
            if ring:
                adv = advected(ring, w, cfg.ring_km, ts)
                if adv is not None:
                    up = adv[0]
                    # Bias-correct the upwind value too: same model, same region.
                    up_c = max(0.0, up + bias * math.exp(-lead / cfg.tau_hours))
                    gap = up_c - corrected
                    corrected += wind_weight(w.speed, cfg.wind_weight) * gap
                    sigma = math.hypot(sigma, 0.5 * gap)
            tr = by_ts.get(ts)
            if stations and tr is not None:
                est = traj_obs(stations, tr)
                if est is not None:
                    pm_up, spread_pm, conf = est
                    up_raw = pm_to_aqi(pm_up)
                    up = up_raw if up is None else max(up, up_raw)
                    # Upwind air thins out on the way: mix its excess over local air in ug/m3.
                    f = math.exp(-tr.path_km / cfg.dilution_km)
                    bg = aqi_to_pm(corrected)
                    arrive = pm_to_aqi(bg + (pm_up - bg) * f)
                    spread = pm_to_aqi(bg + (pm_up + spread_pm - bg) * f) - arrive
                    gap = arrive - corrected
                    # Air that barely moved is the current reading again; bias correction covers it.
                    k = min(1.0, tr.path_km / 30)
                    corrected += cfg.obs_weight * conf * k * gap
                    sigma = math.hypot(sigma, spread, 0.5 * (1 - conf) * abs(gap))
            if w.speed < cfg.stagnant_ms:
                sigma = math.hypot(sigma, cfg.stagnant_sigma * (1 - w.speed / cfg.stagnant_ms))
        mix = blh_at(a.blh, ts)
        if mix is not None and mix < 300 and (w is None or w.speed < cfg.stagnant_ms):
            # Inversion: shallow mixing + light wind traps local pollution near the ground.
            sigma = math.hypot(sigma, cfg.stagnant_sigma * (1 - mix / 300))

        # Learned correction: the average error of past forecasts at this lead time, shrunk toward
        # zero while there's little data (n / (n + learn_shrink)).
        base = corrected
        n, lb, _ = a.learned.get(lead_bucket(lead), (0, 0.0, 0.0))
        if cfg.learn and n:
            corrected = max(0.0, corrected + n / (n + cfg.learn_shrink) * lb)

        a.hours.append(HourForecast(ts, lead, m, corrected, sigma,
                                    prob_exceed(corrected, sigma, cfg.threshold), w, up, by_ts.get(ts),
                                    base, mix))
    return a


LEAD_BUCKETS = [(0, 3), (3, 6), (6, 12), (12, 24), (24, 72)]


def lead_bucket(lead_h: float) -> int:
    return next((i for i, (lo, hi) in enumerate(LEAD_BUCKETS) if lead_h <= hi), len(LEAD_BUCKETS) - 1)


# --------------------------------------------------------------------------- smoke (NOAA HMS)

DENSITY_WEIGHT = {"light": 1, "medium": 2, "heavy": 3}


@dataclass
class Plume:
    density: str  # light / medium / heavy (as analysed from satellite imagery)
    start: Optional[int]
    end: Optional[int]
    rings: List[List[Tuple[float, float]]]  # (lon, lat)
    bbox: Tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max

    @property
    def weight(self) -> int:
        return DENSITY_WEIGHT.get(self.density, 1)


def parse_shp(b: bytes) -> List[Optional[Tuple[Tuple[float, ...], List[List[Tuple[float, float]]]]]]:
    """Polygons from an ESRI .shp file: [(bbox, rings)] in record order (None for null shapes)."""
    out, pos = [], 100
    while pos + 8 <= len(b):
        _, words = struct.unpack(">ii", b[pos:pos + 8])
        c = b[pos + 8:pos + 8 + words * 2]
        pos += 8 + words * 2
        stype = struct.unpack("<i", c[:4])[0] if len(c) >= 4 else 0
        if stype not in (5, 15, 25):  # polygon, polygonZ, polygonM
            out.append(None)
            continue
        bbox = struct.unpack("<4d", c[4:36])
        nparts, npts = struct.unpack("<2i", c[36:44])
        parts = list(struct.unpack(f"<{nparts}i", c[44:44 + 4 * nparts])) + [npts]
        xy = struct.unpack(f"<{2 * npts}d", c[44 + 4 * nparts:44 + 4 * nparts + 16 * npts])
        rings = [[(xy[2 * k], xy[2 * k + 1]) for k in range(parts[i], parts[i + 1])] for i in range(nparts)]
        out.append((bbox, rings))
    return out


def parse_dbf(b: bytes) -> List[Dict[str, str]]:
    """Records from a dBASE .dbf file as {field: text}."""
    nrec = struct.unpack("<I", b[4:8])[0]
    hlen, rlen = struct.unpack("<HH", b[8:12])
    fields, pos = [], 32
    while pos < hlen and b[pos] != 0x0D:
        name = b[pos:pos + 11].split(b"\0")[0].decode("latin-1")
        fields.append((name, b[pos + 16]))
        pos += 32
    out = []
    for r in range(nrec):
        rec = b[hlen + r * rlen:hlen + (r + 1) * rlen]
        if not rec or rec[:1] == b"*":  # deleted
            continue
        row, off = {}, 1
        for name, ln in fields:
            row[name] = rec[off:off + ln].decode("latin-1").strip()
            off += ln
        out.append(row)
    return out


def _hms_time(v: str) -> Optional[int]:
    """HMS times look like '2026301 1510' (year, day of year, HHMM UTC)."""
    try:
        return calendar.timegm(time.strptime(v.strip(), "%Y%j %H%M"))
    except (ValueError, AttributeError):
        return None


def parse_hms_zip(data: bytes) -> List[Plume]:
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = {n.lower().rsplit(".", 1)[-1]: n for n in zf.namelist()}
    shapes = parse_shp(zf.read(names["shp"]))
    rows = parse_dbf(zf.read(names["dbf"])) if "dbf" in names else [{}] * len(shapes)
    plumes = []
    for shape, row in zip(shapes, rows):
        if shape is None:
            continue
        low = {k.lower(): v for k, v in row.items()}
        plumes.append(Plume(low.get("density", "").lower() or "light",
                            _hms_time(low.get("start", "")), _hms_time(low.get("end", "")),
                            shape[1], shape[0]))
    return plumes


_HMS_CACHE: Dict[str, object] = {"fetched": 0.0, "plumes": None}


def fetch_hms(cfg: Config, now: float) -> List[Plume]:
    """Latest HMS smoke analysis (today's and yesterday's UTC files), cached ~50 min."""
    if _HMS_CACHE["plumes"] is not None and now - float(_HMS_CACHE["fetched"]) < 50 * 60:
        return _HMS_CACHE["plumes"]  # type: ignore[return-value]
    patterns = [cfg.hms_url] if cfg.hms_url else HMS_URLS
    plumes: List[Plume] = []
    found = False
    for day in (now, now - 24 * HOUR):
        t = time.gmtime(day)
        for pat in patterns:
            url = time.strftime(pat, t)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "aircast/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    plumes += parse_hms_zip(r.read())
                found = True
                break
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
    if not found:
        raise RuntimeError("no HMS smoke file found for today or yesterday")
    # Keep the latest analysis: plumes ending within 6h of the newest one, and not older than 36h.
    ends = [p.end for p in plumes if p.end]
    if ends:
        latest = max(ends)
        plumes = [p for p in plumes if p.end and p.end >= latest - 6 * HOUR and latest >= now - 36 * HOUR]
    _HMS_CACHE.update(fetched=now, plumes=plumes)
    return plumes


def _inside(x: float, y: float, rings: List[List[Tuple[float, float]]]) -> bool:
    """Even-odd rule over all rings, so holes work."""
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def plume_distance(lat: float, lon: float, p: Plume) -> Tuple[float, float]:
    """(km to the plume edge, bearing to nearest point); 0 km when inside."""
    if _inside(lon, lat, p.rings):
        return 0.0, 0.0
    best, bx, by = float("inf"), 0.0, 0.0
    for ring in p.rings:
        pts = [local_km(lat, lon, la, lo) for lo, la in ring]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            dx, dy = x2 - x1, y2 - y1
            L = dx * dx + dy * dy
            f = 0.0 if L == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / L))
            cx, cy = x1 + f * dx, y1 + f * dy
            d = math.hypot(cx, cy)
            if d < best:
                best, bx, by = d, cx, cy
    return best, math.degrees(math.atan2(bx, by)) % 360


SMOKE_NAMES = {0: "none", 1: "within far radius", 2: "within 100 mi", 3: "within 50 mi",
               4: "overhead", 5: "dense overhead"}


@dataclass
class Smoke:
    level: int
    overhead: List[str]  # densities of plumes directly overhead
    n50: int             # plumes 0-50 mi away (not overhead)
    n100: int            # plumes 50-100 mi away
    nearest_km: Optional[float]
    nearest_brg: float
    score: float         # overhead + 0.5 x within 50 + 0.25 x within 100 + 0.125 x within far radius
    analysed: Optional[int]
    n_far: int = 0       # plumes 100 mi - smoke_far_mi away
    far_mi: float = 200.0
    incoming_eta: Optional[int] = None
    incoming_support: float = 0.0
    incoming_density: str = ""
    incoming_km: float = 0.0
    incoming_brg: float = 0.0
    incoming_layer: str = ""            # "aloft" (700 hPa winds) or "low" (near-ground winds)
    incoming_mix: Optional[float] = None  # mixing height at arrival, m
    mix_now: Optional[float] = None


def assess_smoke(cfg: Config, plumes: List[Plume], trajs: List[Trajectory],
                 trajs_aloft: Optional[List[Trajectory]] = None,
                 blh: Optional[Dict[int, float]] = None, now: Optional[float] = None) -> Smoke:
    over, n50, n100, n_far, score = [], 0, 0, 0, 0.0
    near: Optional[Tuple[float, float]] = None
    for p in plumes:
        # Cheap reject: bbox more than ~600 km away.
        if not (p.bbox[1] - 5.5 < cfg.lat < p.bbox[3] + 5.5):
            continue
        d, brg = plume_distance(cfg.lat, cfg.lon, p)
        if d == 0:
            over.append(p.density)
            score += p.weight
            continue
        if near is None or d < near[0]:
            near = (d, brg)
        if d <= 50 * MI:
            n50 += 1
            score += 0.5 * p.weight
        elif d <= 100 * MI:
            n100 += 1
            score += 0.25 * p.weight
        elif d <= cfg.smoke_far_mi * MI:
            n_far += 1
            score += 0.125 * p.weight
    over_w = sum(DENSITY_WEIGHT.get(d, 1) for d in over)
    level = (5 if over and (over_w >= 3 or len(over) >= 3) else 4 if over
             else 3 if n50 else 2 if n100 else 1 if n_far else 0)
    ends = [p.end for p in plumes if p.end]
    sm = Smoke(level, sorted(over, key=lambda d: -DENSITY_WEIGHT.get(d, 1)), n50, n100,
               near[0] if near else None, near[1] if near else 0.0, score, max(ends) if ends else None,
               n_far=n_far, far_mi=cfg.smoke_far_mi)
    if now is not None:
        sm.mix_now = blh_at(blh or {}, now)

    # Incoming: back-trajectories (next watch_hours) that start inside a plume. Plumes are a
    # column of smoke, so check both the winds aloft (where most of it rides) and near the ground.
    def first_hit(tlist):
        for tr in sorted(tlist, key=lambda t: t.ts):
            if tr.lead_h > cfg.watch_hours or tr.path_km < 10:
                continue
            inside = [next((p.density for p in plumes if _inside(lo, la, p.rings)), None)
                      for la, lo, _ in tr.origins]
            k = sum(d is not None for d in inside)
            if inside[0] is not None and k >= 2 or k >= 3:
                return tr, inside, k
        return None

    hits = [(first_hit(t), layer) for t, layer in ((trajs_aloft or [], "aloft"), (trajs, "low"))]
    hits = [(h, layer) for h, layer in hits if h]
    if hits:
        (tr, inside, k), layer = min(hits, key=lambda x: x[0][0].ts)
        la, lo, _ = tr.origins[0]
        sm.incoming_eta, sm.incoming_support, sm.incoming_layer = tr.ts, k / len(SCENARIOS), layer
        sm.incoming_density = next(d for d in inside if d)
        sm.incoming_km, sm.incoming_brg = distance_bearing(cfg.lat, cfg.lon, la, lo)
        sm.incoming_mix = blh_at(blh or {}, tr.ts)
    return sm


def smoke_details(sm: Smoke) -> str:
    parts = []
    if sm.overhead:
        parts.append(f"overhead: {len(sm.overhead)} ({', '.join(sm.overhead)})")
    parts.append(f"within 50 mi: {sm.n50}")
    parts.append(f"50-100 mi: {sm.n100}")
    parts.append(f"100-{sm.far_mi:.0f} mi: {sm.n_far}")
    if sm.nearest_km is not None and not sm.overhead:
        parts.append(f"nearest {sm.nearest_km / MI:.0f} mi {compass(sm.nearest_brg)}")
    parts.append(f"score {sm.score:.1f}")
    return "Smoke plumes " + "; ".join(parts) + "."


def smoke_line(sm: Smoke) -> str:
    items = []
    if sm.overhead:
        k = len(sm.overhead)
        items.append(f"{k} {'plume' if k == 1 else 'plumes'} overhead ({', '.join(sm.overhead)})")
        items.append(f"{sm.n50} within 50 mi")
    else:
        items.append(f"{sm.n50} {'plume' if sm.n50 == 1 else 'plumes'} within 50 mi")
    items.append(f"{sm.n100} at 50–100 mi")
    items.append(f"{sm.n_far} at 100–{sm.far_mi:.0f} mi")
    items.append(f"score {sm.score:.1f}")
    return "Smoke: " + SEP.join(items)


def decide_smoke(cfg: Config, st: Dict[str, float], sm: Smoke, a: Assessment,
                 now: float) -> Optional["Message"]:
    """st holds notified (level last alerted), incoming (0/1), below_since. Mutated in place."""
    sat = f"🛰️🌎🛰️ {as_of(sm.analysed, now)}" if sm.analysed else ""
    wind = wind_outlook(a)
    notified = int(st.get("notified", 0))
    if sm.level > notified:
        st.update(notified=sm.level, below_since=0)
        near = f"{sm.nearest_km / MI:.0f} mi {compass(sm.nearest_brg)}" if sm.nearest_km is not None else ""
        title = {
            1: f"Smoke plume within {cfg.smoke_far_mi:.0f} mi ({near})",
            2: f"Smoke plume within 100 mi ({near})",
            3: f"Smoke plume within 50 mi ({near})",
            4: f"Smoke plume overhead ({', '.join(sm.overhead)})",
            5: f"Dense smoke overhead ({len(sm.overhead)} plumes: {', '.join(sm.overhead)})",
        }[sm.level]
        arrive, mix = "", mixing_text(sm.mix_now) if sm.level >= 4 else ""
        if sm.incoming_eta and sm.level < 4 and not st.get("incoming"):
            st["incoming"] = 1
            arrive = f"May arrive: ~{clock(sm.incoming_eta, now)}{SEP}{incoming_layer_text(sm)}"
            mix = mixing_text(sm.incoming_mix)
        return Message(headline(title, a), _body(current_line(a), smoke_line(sm), arrive, mix, sat, wind),
                       {1: 2, 2: 3, 3: 3, 4: 3, 5: 4}[sm.level], "fog")
    if sm.level < notified:
        # Only step down after the lower level has held for a while: plumes jitter between analyses.
        if not st.get("below_since"):
            st["below_since"] = now
        elif now - st["below_since"] >= cfg.smoke_clear_hours * HOUR:
            st.update(notified=sm.level, below_since=0)
            if sm.level == 0:
                st["incoming"] = 0
                return Message(headline("Smoke plumes cleared", a),
                               _body(current_line(a),
                                     f"No satellite smoke plume within {cfg.smoke_far_mi:.0f} mi for {cfg.smoke_clear_hours:g}h.",
                                     sat, wind),
                               2, "white_check_mark")
    else:
        st["below_since"] = 0
    if sm.incoming_eta and not st.get("incoming") and sm.level < 4:
        st["incoming"] = 1
        return Message(
            headline(f"Smoke may arrive ~{clock(sm.incoming_eta, now)}", a),
            _body(current_line(a),
                  f"From a {sm.incoming_density} plume now ~{sm.incoming_km / MI:.0f} mi {compass(sm.incoming_brg)}"
                  f"{SEP}{incoming_layer_text(sm)}",
                  smoke_line(sm), mixing_text(sm.incoming_mix), sat, wind),
            3, "fog")
    return None


def incoming_layer_text(sm: Smoke) -> str:
    return "on winds aloft (~10,000 ft)" if sm.incoming_layer == "aloft" else "on near-ground winds"


# --------------------------------------------------------------------------- alert state

CLEAR, WATCH, FORECAST, NOW = "clear", "watch", "forecast", "now"
RANK = {CLEAR: 0, WATCH: 1, FORECAST: 2, NOW: 3}


def next_level(cfg: Config, prev: str, a: Assessment) -> str:
    if a.current > cfg.threshold:
        return NOW
    if a.max_p >= cfg.alert_prob:
        return FORECAST
    # Only drop below forecast once comfortably clear: avoids flapping around the threshold.
    if a.current <= cfg.threshold - cfg.hysteresis and a.max_p < cfg.clear_prob:
        return WATCH if a.threats else CLEAR
    held = FORECAST if prev == NOW else prev
    if a.threats and RANK[held] < RANK[WATCH]:
        return WATCH
    return held


def is_bad(cfg: Config, level: str, a: Assessment) -> bool:
    """Poll faster whenever anything is brewing."""
    smoky = a.smoke is not None and (a.smoke.level > 0 or a.smoke.incoming_eta is not None)
    return (level != CLEAR or a.max_p >= cfg.clear_prob
            or a.current > cfg.threshold - cfg.hysteresis or smoky)


@dataclass
class Message:
    title: str
    body: str
    priority: int
    tags: str


def _fmt_time(ts: int) -> str:
    return time.strftime("%a %H:%M", time.localtime(ts))


SEP = "  ||  "  # separates fields within a message line


def clock(ts: float, now: float) -> str:
    """'1 PM', '11:49 AM', or 'Thu 8 AM' when it's not today (local time; set TZ)."""
    t, n = time.localtime(ts), time.localtime(now)
    hm = time.strftime("%I:%M %p", t).lstrip("0").replace(":00 ", " ")
    return hm if t[:3] == n[:3] else f"{time.strftime('%a', t)} {hm}"


def ago(ts: float, now: float) -> str:
    mins = max(0.0, (now - ts) / 60)
    if mins < 60:
        return f"{mins:.0f} min ago"
    hrs = round(mins / 30) / 2
    return f"{hrs:g} hr ago" if hrs == 1 else f"{hrs:g} hrs ago"


def as_of(ts: float, now: float) -> str:
    return f"{clock(ts, now)} ~ {ago(ts, now)}"


def headline(title: str, a: Assessment) -> str:
    return f"{title}{SEP}~{a.place}" if a.place else title


def current_line(a: Assessment) -> str:
    if a.current_ts is None:
        return f"Current AQI: ~{a.current:.0f} (model estimate){SEP}no monitor in range"
    return f"Current AQI: {a.current:.0f}{SEP}{as_of(a.current_ts, a.now)}"


def wind_text(a: Assessment) -> str:
    w = a.wind_now
    if w is None:
        return ""
    txt = f"Wind {compass(w.direction)} {w.speed * MPH:.0f} mph"
    if a.upwind_now is not None:
        txt += f", upwind air ~{a.upwind_now:.0f}"
    elif w.speed < 0.5:
        txt += " (calm)"
    return txt + "."


def wind_outlook(a: Assessment) -> str:
    """Several time scales so a short spike isn't hidden by averaging, e.g.
    'Wind (mph): 0–6h S 7  ||  6–12h S 9, max 16, gusts 39  ||  12–24h S 16'."""
    parts = []
    for lo, hi in ((0, 6), (6, 12), (12, 24)):
        ws = [w for t, w in sorted(a.surface.items()) if a.now + lo * HOUR < t <= a.now + hi * HOUR]
        if not ws:
            continue
        m = mean_wind(ws)
        mean, peak = m.speed * MPH, max(w.speed for w in ws) * MPH
        txt = f"{lo}–{hi}h {compass(m.direction)} {mean:.0f}"
        if peak >= mean + 5:
            txt += f", max {peak:.0f}"
        gusts = [w.gust for w in ws if w.gust is not None]
        if gusts and max(gusts) * MPH >= 1.5 * peak:
            txt += f", gusts {max(gusts) * MPH:.0f}"
        parts.append(txt)
    return ("Wind (mph): " + SEP.join(parts)) if parts else ""


def threat_text(t: Threat) -> str:
    where = "model" if t.name == "model" else f"AirNow {t.name}"
    return f"AQI {t.aqi:.0f} ({where}) {t.km / MI:.0f} mi {compass(t.bearing)}"


def _body(*lines: str) -> str:
    return "\n".join(line for line in lines if line)


def decide(cfg: Config, prev: str, last_notified: float, a: Assessment) -> Tuple[str, Optional[Message]]:
    """Return (new_level, message-to-send-or-None)."""
    level = next_level(cfg, prev, a)
    thr, hz = int(cfg.threshold), cfg.horizon_hours
    peak = a.peak
    wind = wind_outlook(a)

    if level == NOW and (prev != NOW or (cfg.remind_hours > 0 and a.now - last_notified >= cfg.remind_hours * HOUR)):
        return level, Message(
            headline(f"AQI {a.current:.0f}, above {thr}", a),
            _body(current_line(a),
                  f"Next {hz}h: peak ~{peak.corrected:.0f} around {clock(peak.ts, a.now)}" if peak else "",
                  wind),
            4, "rotating_light")
    if level == FORECAST and RANK[prev] < RANK[FORECAST]:
        first = a.first_exceed(cfg.alert_prob)
        when = clock(first.ts, a.now) if first else "soon"
        return level, Message(
            headline(f"AQI may exceed {thr} by {when}", a),
            _body(current_line(a),
                  f"Chance above {thr} in next {hz}h: {a.max_p:.0%}",
                  f"Peak: ~{peak.corrected:.0f} around {clock(peak.ts, a.now)}" if peak else "",
                  mixing_text(peak.blh) if peak and peak.blh is not None and peak.blh < 300 else "",
                  wind),
            3, "warning")
    if level == WATCH and RANK[prev] < RANK[WATCH]:
        t = a.threats[0]
        where = f"{t.km / MI:.0f} mi {compass(t.bearing)}"
        if t.name == "model":
            src = f"Upwind: AQI ~{t.aqi:.0f} (model), {where}{SEP}model forecast"
        else:
            src = f"Upwind: AQI {t.aqi:.0f} at {t.name}, {where}"
            if t.obs_ts:
                src += f"{SEP}{as_of(t.obs_ts, a.now)}"
        return level, Message(
            headline(f"Possible AQI rise: {t.aqi:.0f} upwind, {where}", a),
            _body(current_line(a), src,
                  f"Could arrive: ~{clock(t.eta, a.now)}"
                  + (f"{SEP}~AQI {t.arrive_aqi:.0f} after mixing on the way" if t.arrive_aqi is not None else ""),
                  wind),
            3, "eyes")
    if level == CLEAR and prev != CLEAR:
        if prev == WATCH:
            return level, Message(headline("Upwind watch over", a),
                                  _body(current_line(a), "Upwind air has cleared or the wind has shifted away.", wind),
                                  2, "white_check_mark")
        return level, Message(headline(f"AQI back to {a.current:.0f}", a),
                              _body(current_line(a), f"Not expected above {thr} in the next {hz}h.", wind),
                              2, "white_check_mark")
    return level, None


# --------------------------------------------------------------------------- storage

class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS residual (ts INTEGER PRIMARY KEY, observed REAL, model REAL);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS forecast (
                issued INTEGER, valid INTEGER, lead REAL, model REAL, base REAL,
                corrected REAL, sigma REAL, p REAL, PRIMARY KEY (issued, valid));
        """)

    def add_residual(self, ts: int, observed: float, model: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO residual VALUES (?,?,?)", (ts, observed, model))
        self.db.execute("DELETE FROM residual WHERE ts < ?", (ts - 90 * 24 * HOUR,))
        self.db.commit()

    def residuals(self, since: float) -> List[Tuple[int, float]]:
        rows = self.db.execute("SELECT ts, observed - model FROM residual WHERE ts >= ? ORDER BY ts", (since,))
        return [(int(t), float(r)) for t, r in rows]

    def log_forecast(self, a: Assessment, keep_days: int) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO forecast VALUES (?,?,?,?,?,?,?,?)",
            [(a.now, h.ts, h.lead_h, h.model, h.base if h.base is not None else h.corrected,
              h.corrected, h.sigma, h.p_exceed) for h in a.hours])
        self.db.execute("DELETE FROM forecast WHERE issued < ?", (a.now - keep_days * 24 * HOUR,))
        self.db.commit()

    def pairs(self, since: float) -> List[Tuple[float, float, float, float, float, float]]:
        """(lead, base, corrected, sigma, p, observed) for every logged forecast hour now observed."""
        return list(self.db.execute(
            "SELECT f.lead, f.base, f.corrected, f.sigma, f.p, r.observed FROM forecast f "
            "JOIN residual r ON r.ts = f.valid WHERE f.issued >= ?", (since,)))

    def learned(self, since: float) -> Dict[int, Tuple[int, float, float]]:
        """Per lead bucket: (n, mean error observed - base, RMSE)."""
        acc: Dict[int, List[float]] = {}
        for lead, base, _, _, _, obs in self.pairs(since):
            acc.setdefault(lead_bucket(lead), []).append(obs - base)
        return {b: (len(e), sum(e) / len(e), math.sqrt(sum(x * x for x in e) / len(e))) for b, e in acc.items()}

    def get(self, k: str, default: str) -> str:
        row = self.db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return row[0] if row else default

    def set(self, k: str, v: object) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, str(v)))
        self.db.commit()


# --------------------------------------------------------------------------- notify

def notify(cfg: Config, msg: Message) -> None:
    log.info("NOTIFY [%s] %s - %s", msg.tags, msg.title, msg.body)
    if not (cfg.ntfy_url and cfg.ntfy_topic):
        log.warning("NTFY_URL/NTFY_TOPIC not set; notification only logged")
        return
    headers = {
        # HTTP headers must be latin-1; keep titles ASCII.
        "Title": msg.title.encode("ascii", "replace").decode(),
        "Priority": str(msg.priority),
        "Tags": msg.tags,
    }
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    if cfg.ntfy_icon:  # large notification icon on Android (JPEG/PNG URL)
        headers["Icon"] = cfg.ntfy_icon
    url = cfg.ntfy_url.rstrip("/") + "/" + urllib.parse.quote(cfg.ntfy_topic)
    req = urllib.request.Request(url, data=msg.body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


# --------------------------------------------------------------------------- main loop

def tick(cfg: Config, store: Store, send: bool = True) -> Assessment:
    now = time.time()
    model = fetch_model(cfg)
    stations: List[Station] = []
    try:
        stations = fetch_stations(cfg)
        if cfg.airnow_key:
            log.info("AirNow: %d monitors within %.0f km", len(stations), cfg.upwind_km)
    except Exception as e:
        log.warning("AirNow monitor fetch failed: %s", e)
    try:
        obs = fetch_observation(cfg)
    except Exception as e:  # AirNow is optional; fall back to the nearest monitor, then model-only.
        log.warning("AirNow current observation failed (%s); using nearest monitor", e)
        obs = None
    if obs is None:
        obs = nearest_observation(cfg, stations)
    if obs is not None:
        m = interp(model, obs[0])
        if m is not None:
            store.add_residual(obs[0], obs[1], m)

    wind_now, surface, ring = None, {}, None
    trajs: List[Trajectory] = []
    trajs_aloft: List[Trajectory] = []
    blh: Dict[int, float] = {}
    if cfg.use_wind:
        try:  # Wind refines the forecast; never let it block alerts.
            wd = fetch_wind(cfg)
            wind_now, surface, blh = wd.current, wd.surface, wd.blh
            trajs = back_trajectories(cfg, now, wd.transport, max(cfg.watch_hours, cfg.horizon_hours))
            trajs_aloft = back_trajectories(cfg, now, wd.aloft, cfg.watch_hours)
            ring = fetch_ring(cfg, cfg.ring_km)
        except Exception as e:
            log.warning("wind/upwind fetch failed: %s", e)
    origin_model: Dict[int, float] = {}
    picks = [tr for tr in trajs if round(tr.lead_h) % 3 == 0 and tr.path_km >= 30]
    if picks:
        try:  # model AQI where the air arriving every 3h is now (works without AirNow)
            for tr, series in zip(picks, fetch_points(cfg, [o[:2] for o in (t.origins[0] for t in picks)])):
                v = interp(series, now)
                if v is not None:
                    origin_model[tr.ts] = v
        except Exception as e:
            log.warning("trajectory-origin model fetch failed: %s", e)
    plumes = None
    if cfg.smoke:
        try:
            plumes = fetch_hms(cfg, now)
        except Exception as e:
            log.warning("HMS smoke fetch failed: %s", e)

    learned = store.learned(now - cfg.log_days * 24 * HOUR) if cfg.learn else {}
    a = assess(cfg, model, store.residuals(now - 7 * 24 * HOUR), obs, now, wind_now, surface,
               ring, stations, trajs, origin_model, plumes, trajs_aloft, blh, learned)
    peak = a.peak
    log.info("current=%.0f (%s, as of %s) bias=%+.1f sigma0=%.1f max_p=%.0f%% peak=%s %s",
             a.current, a.current_source, as_of(a.current_ts, now) if a.current_ts else "n/a",
             a.bias, a.sigma0, a.max_p * 100,
             f"{peak.corrected:.0f}@+{peak.lead_h:.0f}h" if peak else "n/a", wind_text(a))
    for t in a.threats[:3]:
        log.info("upwind: %s, eta %s (%.0f%% of scenarios)", threat_text(t), _fmt_time(t.eta), t.support * 100)
    if a.smoke is not None:
        log.info("smoke: level %d (%s). %s", a.smoke.level, SMOKE_NAMES[a.smoke.level], smoke_details(a.smoke))
    if a.learned:
        log.info("learned: %s", learned_text(cfg, a.learned))
    if not send:
        return a

    store.log_forecast(a, cfg.log_days)

    if a.smoke is not None:
        st = {k: float(store.get("smoke_" + k, "0")) for k in ("notified", "incoming", "below_since")}
        smsg = decide_smoke(cfg, st, a.smoke, a, now)
        if smsg:
            notify(cfg, smsg)
        for k, v in st.items():
            store.set("smoke_" + k, v)

    prev = store.get("level", CLEAR)
    last = float(store.get("last_notified", "0"))
    level, msg = decide(cfg, prev, last, a)
    if msg:
        notify(cfg, msg)
        store.set("last_notified", a.now)
    if level != prev:
        log.info("level %s -> %s", prev, level)
    store.set("level", level)
    return a


def learned_text(cfg: Config, learned: Dict[int, Tuple[int, float, float]]) -> str:
    out = []
    for b, (lo, hi) in enumerate(LEAD_BUCKETS):
        if b in learned:
            n, bias, _ = learned[b]
            out.append(f"{lo}-{hi}h {n / (n + cfg.learn_shrink) * bias:+.1f} (n={n})")
    return ", ".join(out)


def print_score(cfg: Config, store: Store, days: float) -> None:
    """How good have the forecasts been? Errors by lead time, and threshold-event skill."""
    rows = store.pairs(time.time() - days * 24 * HOUR)
    print(f"Forecast-hours with observations, last {days:g} days: {len(rows)}")
    if not rows:
        print("Nothing to score yet: forecasts are logged each check and matched once AirNow reports.")
        return
    print(f"\n{'lead':>8} {'n':>6} {'bias':>6} {'MAE':>6} {'RMSE':>6}   (observed - forecast, AQI)")
    for b, (lo, hi) in enumerate(LEAD_BUCKETS):
        e = [obs - corr for lead, _, corr, _, _, obs in rows if lead_bucket(lead) == b]
        if e:
            print(f"{lo:>3}-{hi:<3}h {len(e):6} {sum(e) / len(e):+6.1f} {sum(map(abs, e)) / len(e):6.1f} "
                  f"{math.sqrt(sum(x * x for x in e) / len(e)):6.1f}")

    thr = cfg.threshold
    ev = [(p, obs > thr) for lead, _, _, _, p, obs in rows if lead <= cfg.horizon_hours]
    hit = sum(1 for p, o in ev if p >= cfg.alert_prob and o)
    miss = sum(1 for p, o in ev if p < cfg.alert_prob and o)
    fa = sum(1 for p, o in ev if p >= cfg.alert_prob and not o)
    cn = len(ev) - hit - miss - fa
    print(f"\nAbove {thr:.0f}, next {cfg.horizon_hours}h (alert at P >= {cfg.alert_prob:.0%}):")
    print(f"  hits {hit}  misses {miss}  false alarms {fa}  correct quiet {cn}")
    if hit + miss:
        print(f"  caught {hit / (hit + miss):.0%} of exceedances")
    if hit + fa:
        print(f"  {fa / (hit + fa):.0%} of alerts were false alarms")
    brier = sum((p - o) ** 2 for p, o in ev) / len(ev)
    print(f"  Brier score {brier:.3f} (0 = perfect)")
    print("\n  Calibration: when it said ...  it happened")
    for lo in range(0, 100, 20):
        grp = [o for p, o in ev if lo / 100 <= p < (lo + 20) / 100 or (lo == 80 and p == 1)]
        if grp:
            print(f"    {lo:3}-{lo + 20}%  {sum(grp) / len(grp):6.0%}  (n={len(grp)})")
    if cfg.learn:
        learned = store.learned(time.time() - cfg.log_days * 24 * HOUR)
        if learned:
            print(f"\nLearned corrections now applied: {learned_text(cfg, learned)}")


def run_forever(cfg: Config, store: Store) -> None:
    failures = 0
    while True:
        minutes = cfg.poll_bad_minutes
        try:
            a = tick(cfg, store)
            if not is_bad(cfg, store.get("level", CLEAR), a):
                minutes = cfg.poll_good_minutes
            if failures >= cfg.fail_alert_after:
                notify(cfg, Message("aircast recovered", "Data fetches are working again.", 2, "white_check_mark"))
            failures = 0
        except Exception as e:
            failures += 1
            log.error("tick failed (%d in a row): %s", failures, e)
            if failures == cfg.fail_alert_after:
                try:
                    notify(cfg, Message("aircast can't get data",
                                        f"{failures} failed fetches in a row. Last error: {e}", 3, "x"))
                except Exception as ne:
                    log.error("notify failed: %s", ne)
        log.info("next check in %.0f min", minutes)
        time.sleep(minutes * 60)


def print_status(cfg: Config, a: Assessment) -> None:
    print(f"Current AQI: {a.current:.0f} ({a.current_source})  bias={a.bias:+.1f}  sigma0={a.sigma0:.1f}")
    print(wind_text(a) or "Wind: unavailable")
    print(wind_outlook(a))
    for t in a.threats[:5]:
        print(f"Upwind: {threat_text(t)}, could arrive ~{_fmt_time(t.eta)} "
              f"({t.support * len(SCENARIOS):.0f}/{len(SCENARIOS)} scenarios)")
    if a.smoke is not None:
        sm = a.smoke
        print(f"Smoke: level {sm.level} ({SMOKE_NAMES[sm.level]}). {smoke_details(sm)}")
        if sm.incoming_eta:
            print(f"  incoming ~{_fmt_time(sm.incoming_eta)} from a {sm.incoming_density} plume "
                  f"{sm.incoming_km / MI:.0f} mi {compass(sm.incoming_brg)}")
    else:
        print("Smoke: unavailable")
    if a.smoke is not None and a.smoke.incoming_eta:
        print(f"  {incoming_layer_text(a.smoke)}; {mixing_text(a.smoke.incoming_mix) or 'mixing height unknown'}")
    print(mixing_text(blh_at(a.blh, a.now)) or "Mixing height: unavailable")
    if a.learned:
        print(f"Learned corrections: {learned_text(cfg, a.learned)}")
    print(f"Threshold {cfg.threshold:.0f}; max chance of exceeding in next {cfg.horizon_hours}h: {a.max_p:.0%}\n")
    print(f"{'time':>10} {'lead':>5} {'wind mph':>9} {'air from':>12} {'upwind':>6} {'mix ft':>7} "
          f"{'model':>6} {'corr':>6} {'+/-':>5} {'P>thr':>6}")
    for h in a.hours:
        wind = f"{compass(h.wind.direction)} {h.wind.speed * MPH:.0f}" if h.wind else "-"
        src = "-"
        if h.traj is not None:
            km, brg = distance_bearing(cfg.lat, cfg.lon, *h.traj.origins[0][:2])
            src = f"{km / MI:.0f} mi {compass(brg)}"
        up = f"{h.upwind:.0f}" if h.upwind is not None else "-"
        mix = f"{h.blh * 3.281:,.0f}" if h.blh is not None else "-"
        bar = "#" * int(h.p_exceed * 20)
        print(f"{_fmt_time(h.ts):>10} {h.lead_h:4.0f}h {wind:>9} {src:>12} {up:>6} {mix:>7} {h.model:6.0f} "
              f"{h.corrected:6.0f} {h.sigma:5.1f} {h.p_exceed:6.0%} {bar}")


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["run", "once", "status", "score", "test-notify"], nargs="?", default="run")
    p.add_argument("days", nargs="?", type=float, default=30, help="score: how many days back")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()

    if args.command == "test-notify":
        notify(cfg, Message("aircast test", "If you see this, push notifications work.", 3, "test_tube"))
        return
    store = Store(cfg.db_path)
    if args.command == "run":
        run_forever(cfg, store)
    elif args.command == "once":
        tick(cfg, store)
    elif args.command == "score":
        print_score(cfg, store, args.days)
    else:
        print_status(cfg, tick(cfg, store, send=False))


if __name__ == "__main__":
    main()
