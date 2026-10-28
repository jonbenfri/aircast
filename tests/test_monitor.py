import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import aqi_monitor as m  # noqa: E402

H = m.HOUR
NOW = 1_761_600_000 // H * H + 600  # 10 min past an hour
LAT, LON = 40.0, -100.0  # arbitrary test location


def cfg(**kw):
    c = m.Config(lat=LAT, lon=LON)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def flat(value, hours=range(-24, 49)):
    return [(NOW // H * H + h * H, float(value)) for h in hours]


def ramp(start, slope, hours=range(-24, 49)):
    base = NOW // H * H
    return [(base + h * H, float(start + slope * max(0, h))) for h in hours]


def winds(speed, direction, hours=range(-24, 49)):
    base = NOW // H * H
    return {base + h * H: m.Wind(speed, direction) for h in hours}


def station(bearing, km, aqi):
    la, lo = m.destination(LAT, LON, bearing, km)
    return m.Station(la, lo, aqi, f"st{bearing}")


class MathTest(unittest.TestCase):
    def test_prob_exceed_symmetric(self):
        self.assertAlmostEqual(m.prob_exceed(35, 5, 35), 0.5)
        self.assertGreater(m.prob_exceed(45, 5, 35), 0.95)
        self.assertLess(m.prob_exceed(20, 5, 35), 0.01)

    def test_bias_prefers_recent(self):
        res = [(NOW - 10 * H, -20.0), (NOW - H, 10.0)]
        self.assertGreater(m.estimate_bias(res, NOW), 5)

    def test_destination_roundtrip(self):
        la, lo = m.destination(LAT, LON, 180, 50)
        km, brg = m.distance_bearing(LAT, LON, la, lo)
        self.assertAlmostEqual(km, 50, delta=0.1)
        self.assertAlmostEqual(brg, 180, delta=0.5)

    def test_mean_wind_vector(self):
        w = m.mean_wind([m.Wind(5, 170), m.Wind(5, 190)])
        self.assertAlmostEqual(w.direction, 180, delta=0.1)
        self.assertLess(m.mean_wind([m.Wind(5, 0), m.Wind(5, 180)]).speed, 1e-9)

    def test_nearest_observation_skips_near_road(self):
        road = station(90, 5, 60); road.name = "Downtown Near-road"
        park = station(270, 12, 20); park.name = "City Park"
        obs = m.nearest_observation(cfg(), [road, park])
        self.assertEqual(obs[1], 20)
        self.assertEqual(m.nearest_observation(cfg(), [road])[1], 60)  # still used if it's all we have

    def test_compass(self):
        self.assertEqual(m.compass(185), "S")
        self.assertEqual(m.compass(350), "N")


class AssessTest(unittest.TestCase):
    def test_clean_air_stays_clear(self):
        a = m.assess(cfg(use_wind=False), flat(15), [], None, NOW)
        self.assertLess(a.max_p, 0.05)

    def test_model_ramp_triggers(self):
        a = m.assess(cfg(), ramp(20, 3), [], None, NOW)  # 20 -> 56 over 12h
        self.assertGreater(a.max_p, 0.9)
        self.assertIsNotNone(a.first_exceed(0.5))

    def test_observation_bias_lifts_forecast(self):
        res = [(NOW - h * H, 15.0) for h in range(6)]  # model reads 15 low
        a = m.assess(cfg(), flat(25), res, (NOW, 40.0, "x"), NOW)
        self.assertEqual(a.current, 40.0)
        self.assertGreater(a.hours[0].corrected, 35)

    def test_smoke_south_with_south_wind(self):
        """The motivating case: model misses smoke 40-80 km south, wind from S at 5 m/s."""
        stations = [station(180, 40, 140), station(175, 60, 160), station(190, 80, 120),
                    station(0, 40, 15)]  # clean station to the north must be ignored
        a = m.assess(cfg(), flat(20), [], None, NOW,
                     m.Wind(5, 180), winds(5, 180), {b: flat(20) for b in m.RING_BEARINGS}, stations)
        first = a.first_exceed(0.5)
        self.assertIsNotNone(first, "should warn even though the model says 20")
        self.assertLessEqual(first.lead_h, 4)  # 40 km at 18 km/h ~ 2.2h
        self.assertGreater(a.upwind_now, 100)

    def test_smoke_south_but_wind_from_north(self):
        stations = [station(180, 40, 140), station(175, 60, 160)]
        a = m.assess(cfg(), flat(20), [], None, NOW,
                     m.Wind(5, 0), winds(5, 0), {b: flat(20) for b in m.RING_BEARINGS}, stations)
        self.assertLess(a.max_p, 0.2)

    def test_wind_shift_later_brings_smoke(self):
        """Wind from N now, turning S in 6h: smoke should arrive after the shift."""
        base = NOW // H * H
        hourly = {base + h * H: m.Wind(5, 0 if h < 6 else 180) for h in range(-24, 49)}
        stations = [station(180, 30, 150)]
        a = m.assess(cfg(horizon_hours=18), flat(20), [], None, NOW,
                     m.Wind(5, 0), hourly, None, stations)
        early = [h for h in a.hours if h.lead_h <= 4]
        self.assertTrue(all(h.p_exceed < 0.2 for h in early))
        self.assertGreater(a.max_p, 0.5)

    def test_model_ring_upwind(self):
        ring = {b: flat(20) for b in m.RING_BEARINGS}
        ring[180] = flat(90)
        a = m.assess(cfg(), flat(25), [], None, NOW, m.Wind(6, 180), winds(6, 180), ring)
        self.assertGreater(a.hours[3].corrected, 40)

    def test_calm_ignores_direction_but_widens_sigma(self):
        ring = {b: flat(20) for b in m.RING_BEARINGS}
        ring[180] = flat(90)
        calm = m.assess(cfg(), flat(25), [], None, NOW, m.Wind(0.2, 180), winds(0.2, 180), ring)
        windy = m.assess(cfg(), flat(25), [], None, NOW, m.Wind(6, 0), winds(6, 0),
                         {b: flat(25) for b in m.RING_BEARINGS})
        self.assertAlmostEqual(calm.hours[2].corrected, 25, delta=0.5)
        self.assertGreater(calm.hours[2].sigma, windy.hours[2].sigma)


class WatchTest(unittest.TestCase):
    """AQI 15 here, AQI 45 about 100 miles south: alert depends on the wind."""

    def run_case(self, wind_now, hourly):
        stations = [station(180, 160, 45)]
        a = m.assess(cfg(), flat(15), [], None, NOW, wind_now, hourly,
                     {b: flat(15) for b in m.RING_BEARINGS}, stations)
        return a, m.decide(cfg(), m.CLEAR, 0, a)

    def test_south_wind_alerts(self):
        a, (lvl, msg) = self.run_case(m.Wind(5, 180), winds(5, 180))
        self.assertTrue(a.threats)
        self.assertIn(lvl, (m.WATCH, m.FORECAST))
        self.assertIsNotNone(msg)
        if lvl == m.WATCH:
            self.assertIn("99 mi S", msg.title)
            self.assertIn("Check the weather", msg.body)

    def test_north_wind_quiet(self):
        a, (lvl, msg) = self.run_case(m.Wind(5, 0), winds(5, 0))
        self.assertEqual(a.threats, [])
        self.assertEqual((lvl, msg), (m.CLEAR, None))

    def test_wind_turning_south_soon_counts(self):
        base = NOW // H * H
        hourly = {base + h * H: m.Wind(5, 270 if h < 1 else 185) for h in range(-24, 49)}
        a, (lvl, msg) = self.run_case(m.Wind(5, 270), hourly)
        self.assertTrue(a.threats)
        self.assertIsNotNone(msg)

    def test_model_origin_without_airnow(self):
        trajs = m.back_trajectories(cfg(), NOW, winds(5, 180), 24)
        origin_model = {tr.ts: (60.0 if tr.lead_h >= 6 else 15.0) for tr in trajs}
        a = m.assess(cfg(), flat(15), [], None, NOW, m.Wind(5, 180), winds(5, 180), None, [],
                     trajs, origin_model)
        self.assertEqual(a.threats[0].name, "model")
        self.assertEqual(m.compass(a.threats[0].bearing), "S")

    def test_watch_clears_when_wind_turns(self):
        a, _ = self.run_case(m.Wind(5, 0), winds(5, 0))
        lvl, msg = m.decide(cfg(), m.WATCH, NOW, a)
        self.assertEqual(lvl, m.CLEAR)
        self.assertIn("watch over", msg.title)

    def test_polling_interval(self):
        quiet, _ = self.run_case(m.Wind(5, 0), winds(5, 0))
        self.assertFalse(m.is_bad(cfg(), m.CLEAR, quiet))
        self.assertTrue(m.is_bad(cfg(), m.WATCH, quiet))


class TrajectoryTest(unittest.TestCase):
    def test_constant_wind(self):
        trajs = m.back_trajectories(cfg(), NOW, winds(5, 180), 6)
        tr = next(t for t in trajs if round(t.lead_h) == 4)
        e, n = m.local_km(LAT, LON, *tr.origins[0][:2])
        self.assertAlmostEqual(n, -5 * 3.6 * tr.lead_h, delta=1)  # air came from the south
        self.assertAlmostEqual(e, 0, delta=1)
        self.assertEqual(len(tr.origins), len(m.SCENARIOS))

    def test_spike_is_not_smoothed_away(self):
        """A 3h burst of strong south wind moves air far; averaging with calm hours would not show where."""
        base = NOW // H * H
        hourly = {base + h * H: m.Wind(12 if 1 <= h <= 3 else 1, 180 if 1 <= h <= 3 else 90)
                  for h in range(-24, 49)}
        tr = next(t for t in m.back_trajectories(cfg(), NOW, hourly, 6) if round(t.lead_h) == 5)
        e, n = m.local_km(LAT, LON, *tr.origins[0][:2])
        self.assertLess(n, -100)       # most of the trip was the southerly burst
        self.assertLess(abs(e), 20)    # the light easterly hours barely matter

    def test_scenarios_spread(self):
        tr = next(t for t in m.back_trajectories(cfg(), NOW, winds(5, 180), 12) if round(t.lead_h) == 10)
        lons = [o[1] for o in tr.origins]
        self.assertGreater(max(lons) - min(lons), 0.5)  # +/-20 deg fans out


def square(lat, lon, half_km, density="light"):
    dlat, dlon = half_km / 110.57, half_km / (111.32 * math.cos(math.radians(lat)))
    ring = [(lon - dlon, lat - dlat), (lon - dlon, lat + dlat), (lon + dlon, lat + dlat),
            (lon + dlon, lat - dlat), (lon - dlon, lat - dlat)]
    return m.Plume(density, None, NOW, [ring], (lon - dlon, lat - dlat, lon + dlon, lat + dlat))


def plume_at(bearing, km, half_km=20, density="light"):
    la, lo = m.destination(LAT, LON, bearing, km)
    return square(la, lo, half_km, density)


class SmokeTest(unittest.TestCase):
    def level(self, plumes, trajs=()):
        return m.assess_smoke(cfg(), plumes, list(trajs))

    def test_levels(self):
        self.assertEqual(self.level([]).level, 0)
        self.assertEqual(self.level([plume_at(180, 250)]).level, 0)
        self.assertEqual(self.level([plume_at(180, 140)]).level, 1)   # edge ~75 mi
        self.assertEqual(self.level([plume_at(180, 70)]).level, 2)    # edge ~31 mi
        self.assertEqual(self.level([square(LAT, LON, 30)]).level, 3)
        dense = self.level([square(LAT, LON, 30, "medium"), square(LAT, LON, 50)])
        self.assertEqual(dense.level, 4)
        self.assertEqual(dense.overhead, ["medium", "light"])
        self.assertEqual(self.level([square(LAT, LON, 30, "heavy")]).level, 4)

    def test_overhead_plus_second_plume_nearby(self):
        sm = self.level([square(LAT, LON, 30), plume_at(270, 80)])
        self.assertEqual(sm.level, 3)
        self.assertEqual(sm.n50, 1)
        self.assertAlmostEqual(sm.score, 1.5)

    def test_nearest_distance_and_direction(self):
        sm = self.level([plume_at(180, 70)])
        self.assertAlmostEqual(sm.nearest_km, 50, delta=2)
        self.assertEqual(m.compass(sm.nearest_brg), "S")

    def test_incoming_on_south_wind(self):
        trajs = m.back_trajectories(cfg(), NOW, winds(5, 180), 12)
        sm = self.level([plume_at(180, 150, half_km=40)], trajs)  # edge 110 km = 68 mi
        self.assertEqual(sm.level, 1)
        self.assertIsNotNone(sm.incoming_eta)
        self.assertAlmostEqual((sm.incoming_eta - NOW) / H, 110 / 18, delta=1.5)  # 5 m/s = 18 km/h
        msg = m.decide_smoke(cfg(), {"notified": 1}, sm, 20, NOW)
        self.assertTrue(msg.title.startswith("Smoke may arrive"))
        north = m.back_trajectories(cfg(), NOW, winds(5, 0), 12)
        self.assertIsNone(self.level([plume_at(180, 150, half_km=40)], north).incoming_eta)

    def test_alert_flow(self):
        c, st = cfg(), {}
        msg = m.decide_smoke(c, st, self.level([plume_at(180, 70)]), 20, NOW)
        self.assertIn("within 50 mi", msg.title)
        self.assertIsNone(m.decide_smoke(c, st, self.level([plume_at(180, 140)]), 20, NOW + H))  # step down: quiet
        self.assertIsNone(m.decide_smoke(c, st, self.level([plume_at(180, 70)]), 20, NOW + 2 * H))  # back up: no repeat
        msg = m.decide_smoke(c, st, self.level([square(LAT, LON, 30)]), 20, NOW + 3 * H)
        self.assertIn("overhead", msg.title)
        self.assertIsNone(m.decide_smoke(c, st, self.level([]), 20, NOW + 4 * H))
        msg = m.decide_smoke(c, st, self.level([]), 20, NOW + 8 * H)
        self.assertEqual(msg.title, "Smoke plumes cleared")
        self.assertEqual(st["notified"], 0)


def _shapefile_zip(polys):
    """Minimal ESRI shapefile (.shp + .dbf) with Density/Start/End fields, zipped like HMS."""
    import io as _io, struct as _st, zipfile as _zf
    recs = b""
    for n, (ring, _) in enumerate(polys, 1):
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        c = _st.pack("<i4d2i", 5, min(xs), min(ys), max(xs), max(ys), 1, len(ring)) + _st.pack("<i", 0)
        c += b"".join(_st.pack("<2d", *p) for p in ring)
        recs += _st.pack(">2i", n, len(c) // 2) + c
    shp = _st.pack(">i", 9994) + b"\0" * 20 + _st.pack(">i", (100 + len(recs)) // 2) + _st.pack("<2i", 1000, 5)
    shp += b"\0" * (100 - len(shp)) + recs
    fields = [("Satellite", 10), ("Start", 12), ("End", 12), ("Density", 6)]
    rlen = 1 + sum(l for _, l in fields)
    hdr = _st.pack("<4BIHH", 3, 126, 1, 1, len(polys), 32 + 32 * len(fields) + 1, rlen) + b"\0" * 20
    for name, l in fields:
        hdr += name.encode().ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([l, 0]) + b"\0" * 14
    hdr += b"\r"
    body = b"".join(b" " + b"".join(v.encode().ljust(l) for v, (_, l) in zip(
        ["GOES-EAST", "2026301 1500", "2026301 1700", dens], fields)) for _, dens in polys)
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("hms_smoke20261028.shp", shp)
        z.writestr("hms_smoke20261028.dbf", hdr + body + b"\x1a")
    return buf.getvalue()


class HmsParseTest(unittest.TestCase):
    def test_roundtrip(self):
        p = square(LAT, LON, 30)
        data = _shapefile_zip([(p.rings[0], "Medium"), (plume_at(180, 70).rings[0], "Light")])
        plumes = m.parse_hms_zip(data)
        self.assertEqual([q.density for q in plumes], ["medium", "light"])
        self.assertEqual(plumes[0].end - plumes[0].start, 2 * H)
        self.assertEqual(m.assess_smoke(cfg(), plumes, []).level, 3)


class StateTest(unittest.TestCase):
    def _a(self, current, max_p_mean):
        a = m.Assessment(NOW, current, "model", 0, 8)
        a.hours = [m.HourForecast(NOW + H, 1, max_p_mean, max_p_mean, 5,
                                  m.prob_exceed(max_p_mean, 5, 35))]
        return a

    def test_transitions(self):
        c = cfg()
        lvl, msg = m.decide(c, m.CLEAR, 0, self._a(20, 50))
        self.assertEqual((lvl, msg.priority), (m.FORECAST, 3))
        lvl, msg = m.decide(c, lvl, NOW, self._a(40, 50))
        self.assertEqual((lvl, msg.priority), (m.NOW, 4))
        lvl, msg = m.decide(c, lvl, NOW, self._a(41, 50))
        self.assertIsNone(msg)  # no spam within remind window
        lvl, msg = m.decide(c, lvl, NOW - 7 * H, self._a(41, 50))
        self.assertIsNotNone(msg)  # reminder after 6h
        lvl, msg = m.decide(c, lvl, NOW, self._a(33, 30))
        self.assertEqual(lvl, m.FORECAST)  # inside hysteresis band: hold
        self.assertIsNone(msg)
        lvl, msg = m.decide(c, lvl, NOW, self._a(20, 15))
        self.assertEqual((lvl, msg.priority), (m.CLEAR, 2))


if __name__ == "__main__":
    unittest.main()
