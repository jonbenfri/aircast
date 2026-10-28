#!/usr/bin/env python3
"""AQI monitor: forecasts when US AQI near you will exceed a threshold and
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

Upwind monitors (AirNow, needs key): models often miss wildfire smoke, so
  real stations within ~150 km also count. For forecast hour h we average the
  wind vector from now to h, keep stations inside the upwind cone, and weight
  each by alignment and by how closely its travel time (distance / speed)
  matches h. Assuming the plume persists, their current readings say what
  arrives at h:
    corrected(h) += obs_weight * confidence * w(speed) * (upwind_obs(h) - corrected(h))

Stdlib only (Python 3.8+). Configured through environment variables; see
deploy/aqi-monitor.env.example.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("aqi-monitor")

OPEN_METEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
OPEN_METEO_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
# The old /aq/observation/latLong/current/ was retired 2026-09-30 (HTTP 410).
AIRNOW_URL = "https://www.airnowapi.org/aq/observation/current/ziplatlong/"
AIRNOW_DATA_URL = "https://www.airnowapi.org/aq/data/"
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
    upwind_km: float = 170.0
    watch_aqi: Optional[float] = None
    watch_cone_deg: float = 45.0
    obs_weight: float = 0.6
    airnow_key: Optional[str] = None
    airnow_distance: int = 25
    ntfy_url: Optional[str] = None
    ntfy_topic: Optional[str] = None
    ntfy_token: Optional[str] = None
    ntfy_icon: Optional[str] = None
    db_path: str = "aqi-monitor.db"

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
            upwind_km=f("AQI_UPWIND_KM", 170),
            watch_aqi=float(_env("AQI_WATCH_AQI")) if _env("AQI_WATCH_AQI") else None,
            watch_cone_deg=f("AQI_WATCH_CONE_DEG", 45),
            obs_weight=f("AQI_OBS_WEIGHT", 0.6),
            airnow_key=_env("AIRNOW_API_KEY"),
            airnow_distance=int(f("AIRNOW_DISTANCE_MILES", 25)),
            ntfy_url=_env("NTFY_URL"),
            ntfy_topic=_env("NTFY_TOPIC"),
            ntfy_token=_env("NTFY_TOKEN"),
            ntfy_icon=_env("NTFY_ICON"),
            db_path=_env("AQI_DB", "aqi-monitor.db"),
        )


# --------------------------------------------------------------------------- data

def _get_json(url: str, params: Dict[str, object], timeout: float = 20):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "aqi-monitor/1.0"})
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


def fetch_wind(cfg: Config) -> Tuple[Optional[Wind], Dict[int, Wind]]:
    """(current wind, hourly wind keyed by unix ts) at your location."""
    data = _get_json(OPEN_METEO_WEATHER_URL, {
        "latitude": cfg.lat,
        "longitude": cfg.lon,
        "current": "wind_speed_10m,wind_direction_10m",
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms",
        "past_days": 1,
        "forecast_days": 3,
        "timeformat": "unixtime",
    })
    h = data["hourly"]
    hourly = {int(t): Wind(float(s), float(d))
              for t, s, d in zip(h["time"], h["wind_speed_10m"], h["wind_direction_10m"])
              if s is not None and d is not None}
    c = data.get("current") or {}
    cur = None
    if c.get("wind_speed_10m") is not None and c.get("wind_direction_10m") is not None:
        cur = Wind(float(c["wind_speed_10m"]), float(c["wind_direction_10m"]))
    return cur, hourly


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
    # AirNow reports the hour of observation; the reading is current to within ~1h.
    ts = int(time.time()) // HOUR * HOUR
    return ts, float(worst["AQI"]), f'{worst.get("ReportingArea", "?")} ({worst.get("ParameterName", "?")})'


@dataclass
class Station:
    lat: float
    lon: float
    aqi: float
    name: str


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
    return int(time.time()) // HOUR * HOUR, st.aqi, f"{st.name}, {km / 1.609:.0f} mi"


def fetch_stations(cfg: Config) -> List[Station]:
    """Latest hourly AQI (worst pollutant) at every AirNow monitor within ~upwind_km.

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
        st = Station(key[0], key[1], float(aqi), str(r.get("SiteName") or r.get("Parameter") or "?"))
        prev = latest.get(key)
        # Newest hour wins; within the same hour keep the worse pollutant.
        if prev is None or utc > prev[0] or (utc == prev[0] and st.aqi > prev[1].aqi):
            latest[key] = (utc, st)
    return [st for _, st in latest.values()]


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


def upwind_obs(stations: List[Station], lat: float, lon: float, wind: Wind,
               lead_h: float) -> Optional[Tuple[float, float]]:
    """(AQI the upwind stations imply arrives at lead_h, confidence 0..1) or None."""
    if wind.speed < 0.5:
        return None
    num = den = 0.0
    width = 1.0 + 0.25 * lead_h  # timing gets fuzzier further out
    for st in stations:
        km, brg = distance_bearing(lat, lon, st.lat, st.lon)
        if km < 5:  # that's essentially "here", handled as the current observation
            continue
        off = abs((brg - wind.direction + 180) % 360 - 180)
        if off >= 60:
            continue
        align = math.cos(math.radians(off * 1.5))  # 1 dead upwind, 0 at 60 deg off
        travel_h = km / (wind.speed * 3.6)
        timing = math.exp(-0.5 * ((travel_h - lead_h) / width) ** 2)
        w = align * timing
        num += w * st.aqi
        den += w
    if den < 0.05:
        return None
    return num / den, min(1.0, den)


@dataclass
class Threat:
    """Bad air upwind that the current or forecast wind points at you."""
    aqi: float
    name: str
    km: float
    bearing: float
    wind: Wind
    eta: int


def find_threats(cfg: Config, now: float, stations: List[Station],
                 far_ring: Optional[Dict[int, List[Tuple[int, float]]]],
                 wind_now: Optional[Wind], wind_hourly: Optional[Dict[int, Wind]],
                 max_travel_h: float = 36.0) -> List[Threat]:
    """Upwind monitors (or model ring points) above watch_aqi, worst first.

    Checked against the current wind and the mean forecast wind over the next
    6h, so a wind that is about to turn toward you also counts.
    """
    limit = cfg.threshold if cfg.watch_aqi is None else cfg.watch_aqi
    winds = [wind_now] if wind_now else []
    nxt = mean_wind([v for t, v in sorted((wind_hourly or {}).items()) if now < t <= now + 6 * HOUR])
    if nxt:
        winds.append(nxt)

    cands: List[Tuple[float, str, float, float]] = []  # aqi, name, km, bearing
    for st in stations:
        km, brg = distance_bearing(cfg.lat, cfg.lon, st.lat, st.lon)
        if 5 <= km <= cfg.upwind_km:
            cands.append((st.aqi, st.name, km, brg))
    for brg, series in (far_ring or {}).items():
        v = interp(series, now)
        if v is not None:
            cands.append((v, "model", cfg.upwind_km, float(brg)))

    best: Dict[str, Threat] = {}
    for aqi, name, km, brg in cands:
        if aqi <= limit:
            continue
        for w in winds:
            if w.speed < 1.0:
                continue
            if abs((brg - w.direction + 180) % 360 - 180) > cfg.watch_cone_deg:
                continue
            travel_h = km / (w.speed * 3.6)
            if travel_h > max_travel_h:
                continue
            t = Threat(aqi, name, km, brg, w, int(now + travel_h * HOUR))
            key = f"{name}@{brg:.0f}"
            if key not in best or t.eta < best[key].eta:
                best[key] = t
    return sorted(best.values(), key=lambda t: -t.aqi)


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
           far_ring: Optional[Dict[int, List[Tuple[int, float]]]] = None) -> Assessment:
    bias = estimate_bias(residuals, now)
    sigma0 = estimate_sigma0(residuals, cfg.sigma0)
    model_now = interp(model, now)
    if model_now is None:
        raise ValueError("model series does not cover the current time")
    if observation is not None:
        current, source = observation[1], f"AirNow, preliminary, {observation[2]}"
    else:
        current, source = model_now + bias, "model"

    a = Assessment(now=int(now), current=current, current_source=source, bias=bias,
                   sigma0=sigma0, wind_now=wind_now)
    a.threats = find_threats(cfg, now, stations or [], far_ring, wind_now, wind_hourly)
    if wind_now and ring:
        adv = advected(ring, wind_now, cfg.ring_km, now)
        a.upwind_now = adv[0] if adv else None
    if wind_now and stations:
        # Worst air currently upwind within ~3h travel: what "if this persists" brings.
        ests = [upwind_obs(stations, cfg.lat, cfg.lon, wind_now, h) for h in (1, 2, 3)]
        vals = [e[0] for e in ests if e]
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
            if stations:
                # Air arriving at h travelled under the winds between now and h.
                path = [wind_now] if wind_now else []
                path += [v for t, v in sorted((wind_hourly or {}).items()) if now < t <= ts]
                mw = mean_wind(path) or w
                est = upwind_obs(stations, cfg.lat, cfg.lon, mw, lead)
                if est is not None:
                    obs_aqi, conf = est
                    up = obs_aqi if up is None else max(up, obs_aqi)
                    gap = obs_aqi - corrected
                    corrected += cfg.obs_weight * conf * min(1.0, mw.speed / 3.0) * gap
                    sigma = math.hypot(sigma, 0.5 * (1 - conf) * abs(gap))
            if w.speed < cfg.stagnant_ms:
                sigma = math.hypot(sigma, cfg.stagnant_sigma * (1 - w.speed / cfg.stagnant_ms))

        a.hours.append(HourForecast(ts, lead, m, corrected, sigma,
                                    prob_exceed(corrected, sigma, cfg.threshold), w, up))
    return a


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
    return (level != CLEAR or a.max_p >= cfg.clear_prob
            or a.current > cfg.threshold - cfg.hysteresis)


@dataclass
class Message:
    title: str
    body: str
    priority: int
    tags: str


def _fmt_time(ts: int) -> str:
    return time.strftime("%a %H:%M", time.localtime(ts))


def wind_text(a: Assessment) -> str:
    w = a.wind_now
    if w is None:
        return ""
    txt = f"Wind {compass(w.direction)} {w.speed:.0f} m/s"
    if a.upwind_now is not None:
        txt += f", upwind air ~{a.upwind_now:.0f}"
    elif w.speed < 0.5:
        txt += " (calm)"
    return txt + "."


# AirNow data-use guidelines: our numbers are estimates, not official forecasts.
DISCLAIMER = "(Personal estimate, not an official forecast. See airnow.gov.)"


def wind_outlook(a: Assessment) -> str:
    """e.g. 'Wind next 12h: S 5 -> SW 4 -> W 3 m/s.'"""
    pts = [h for h in a.hours if h.wind and round(h.lead_h) in (3, 6, 12)]
    if not pts:
        return ""
    return "Wind next 12h: " + " -> ".join(f"{compass(h.wind.direction)} {h.wind.speed:.0f}" for h in pts) + " m/s."


def threat_text(t: Threat) -> str:
    where = "CAMS model" if t.name == "model" else f"AirNow {t.name}, preliminary"
    return f"AQI {t.aqi:.0f} ({where}) {t.km / 1.609:.0f} mi {compass(t.bearing)}"


def decide(cfg: Config, prev: str, last_notified: float, a: Assessment) -> Tuple[str, Optional[Message]]:
    """Return (new_level, message-to-send-or-None)."""
    level = next_level(cfg, prev, a)
    thr = int(cfg.threshold)
    peak = a.peak
    peak_txt = f"Peak ~{peak.corrected:.0f} around {_fmt_time(peak.ts)}." if peak else ""
    peak_txt = f"{peak_txt} {wind_text(a)}".strip()

    if level == NOW and (prev != NOW or (cfg.remind_hours > 0 and a.now - last_notified >= cfg.remind_hours * HOUR)):
        return level, Message(
            title=f"AQI {a.current:.0f} - above {thr} now",
            body=f"Current AQI {a.current:.0f} ({a.current_source}). {peak_txt}".strip(),
            priority=4, tags="rotating_light")
    if level == FORECAST and RANK[prev] < RANK[FORECAST]:
        first = a.first_exceed(cfg.alert_prob)
        when = _fmt_time(first.ts) if first else "soon"
        return level, Message(
            title=f"AQI may exceed {thr} by {when}",
            body=(f"Now {a.current:.0f}. Estimated chance of >{thr} within {cfg.horizon_hours}h: "
                  f"{a.max_p:.0%}. {peak_txt} {DISCLAIMER}"),
            priority=3, tags="warning")
    if level == WATCH and RANK[prev] < RANK[WATCH]:
        t = a.threats[0]
        return level, Message(
            title=f"Possible AQI rise: {t.aqi:.0f} upwind {t.km / 1.609:.0f} mi {compass(t.bearing)}",
            body=(f"Now {a.current:.0f}. {threat_text(t)}. Wind from {compass(t.wind.direction)} "
                  f"{t.wind.speed:.0f} m/s; could arrive ~{_fmt_time(t.eta)} if it persists. "
                  f"{wind_outlook(a)} Check the weather. {DISCLAIMER}").replace("  ", " "),
            priority=3, tags="eyes")
    if level == CLEAR and prev != CLEAR:
        body = (f"Below {thr} and not expected to exceed it in the next {cfg.horizon_hours}h."
                if prev != WATCH else "Upwind air has cleared or the wind has shifted away.")
        return level, Message(
            title=f"AQI back to {a.current:.0f}" if prev != WATCH else f"Upwind watch over (AQI {a.current:.0f})",
            body=body, priority=2, tags="white_check_mark")
    return level, None


# --------------------------------------------------------------------------- storage

class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS residual (ts INTEGER PRIMARY KEY, observed REAL, model REAL);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        """)

    def add_residual(self, ts: int, observed: float, model: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO residual VALUES (?,?,?)", (ts, observed, model))
        self.db.execute("DELETE FROM residual WHERE ts < ?", (ts - 14 * 24 * HOUR,))
        self.db.commit()

    def residuals(self, since: float) -> List[Tuple[int, float]]:
        rows = self.db.execute("SELECT ts, observed - model FROM residual WHERE ts >= ? ORDER BY ts", (since,))
        return [(int(t), float(r)) for t, r in rows]

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

    wind_now, wind_hourly, ring = None, None, None
    if cfg.use_wind:
        try:  # Wind refines the forecast; never let it block alerts.
            wind_now, wind_hourly = fetch_wind(cfg)
            ring = fetch_ring(cfg, cfg.ring_km)
        except Exception as e:
            log.warning("wind/upwind fetch failed: %s", e)
    far_ring = None
    if cfg.use_wind and wind_now is not None:
        try:  # model view of what's out at upwind_km, for the watch (works without AirNow)
            far_ring = fetch_ring(cfg, cfg.upwind_km)
        except Exception as e:
            log.warning("far ring fetch failed: %s", e)

    a = assess(cfg, model, store.residuals(now - 7 * 24 * HOUR), obs, now, wind_now, wind_hourly,
               ring, stations, far_ring)
    peak = a.peak
    log.info("current=%.0f (%s) bias=%+.1f sigma0=%.1f max_p=%.0f%% peak=%s %s",
             a.current, a.current_source, a.bias, a.sigma0, a.max_p * 100,
             f"{peak.corrected:.0f}@+{peak.lead_h:.0f}h" if peak else "n/a", wind_text(a))
    for t in a.threats[:3]:
        log.info("upwind: %s, eta %s", threat_text(t), _fmt_time(t.eta))
    if not send:
        return a

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


def run_forever(cfg: Config, store: Store) -> None:
    failures = 0
    while True:
        minutes = cfg.poll_bad_minutes
        try:
            a = tick(cfg, store)
            if not is_bad(cfg, store.get("level", CLEAR), a):
                minutes = cfg.poll_good_minutes
            if failures >= cfg.fail_alert_after:
                notify(cfg, Message("AQI monitor recovered", "Data fetches are working again.", 2, "white_check_mark"))
            failures = 0
        except Exception as e:
            failures += 1
            log.error("tick failed (%d in a row): %s", failures, e)
            if failures == cfg.fail_alert_after:
                try:
                    notify(cfg, Message("AQI monitor can't get data",
                                        f"{failures} failed fetches in a row. Last error: {e}", 3, "x"))
                except Exception as ne:
                    log.error("notify failed: %s", ne)
        log.info("next check in %.0f min", minutes)
        time.sleep(minutes * 60)


def print_status(cfg: Config, a: Assessment) -> None:
    print(f"Current AQI: {a.current:.0f} ({a.current_source})  bias={a.bias:+.1f}  sigma0={a.sigma0:.1f}")
    print(wind_text(a) or "Wind: unavailable")
    for t in a.threats[:5]:
        print(f"Upwind: {threat_text(t)}, could arrive ~{_fmt_time(t.eta)}")
    print(f"Threshold {cfg.threshold:.0f}; max chance of exceeding in next {cfg.horizon_hours}h: {a.max_p:.0%}\n")
    print(f"{'time':>10} {'lead':>5} {'wind':>8} {'upwind':>6} {'model':>6} {'corr':>6} {'+/-':>5} {'P>thr':>6}")
    for h in a.hours:
        wind = f"{compass(h.wind.direction)} {h.wind.speed:.0f}" if h.wind else "-"
        up = f"{h.upwind:.0f}" if h.upwind is not None else "-"
        bar = "#" * int(h.p_exceed * 20)
        print(f"{_fmt_time(h.ts):>10} {h.lead_h:4.0f}h {wind:>8} {up:>6} {h.model:6.0f} "
              f"{h.corrected:6.0f} {h.sigma:5.1f} {h.p_exceed:6.0%} {bar}")


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["run", "once", "status", "test-notify"], nargs="?", default="run")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()

    if args.command == "test-notify":
        notify(cfg, Message("AQI monitor test", "If you see this, push notifications work.", 3, "test_tube"))
        return
    store = Store(cfg.db_path)
    if args.command == "run":
        run_forever(cfg, store)
    elif args.command == "once":
        tick(cfg, store)
    else:
        print_status(cfg, tick(cfg, store, send=False))


if __name__ == "__main__":
    main()
