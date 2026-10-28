# Roadmap

Ideas and open items, roughly in priority order.

## Before open-sourcing
- Choose a license (MIT, Apache-2.0 or GPL-3.0) and add `LICENSE`.
- GitHub Action that runs `uv run python -m unittest` on every push.
- Short CONTRIBUTING note.

## Alerts
- A different notification icon for each alert type (watch / forecast / now / clear / error).
- Include AirNow's official forecast and any agency advisory in alerts (AirNow data-use guidelines).
- Add NOAA HMS satellite smoke polygons (what fire.airnow.gov shows) as an early-warning signal.

## Smoke map for your area
- A local version of the fire.airnow.gov view, centered on your location (~150-300 mi), with:
  - NOAA HMS smoke plumes (gray polygons; light/medium/heavy density), updated daily
  - active fire detections (HMS / NASA FIRMS hotspots)
  - AirNow monitors colored by AQI category (official AQI colors)
  - wind arrows, with the upwind cone the watch alert uses
- Attach a rendered map image to watch/forecast alerts (ntfy attachment or icon), and/or
  serve it as a small web page.
- Simplest first step: a link in each alert that opens fire.airnow.gov centered on your location.

## Many users
- Same area: one monitor and topic, with a read-only ntfy account per subscriber.
- A few areas: systemd template service (`aqi-monitor@.service`), one env file and topic per area.
- Many areas: one process serving a list of areas, sharing AirNow downloads between nearby areas.
  Watch Open-Meteo's free tier: roughly 18 calls per area per check, about 10k calls a day.
- Per-user location: either a fixed location the user sets, or the phone's location
  (e.g. OwnTracks posting to the server), mapped to the nearest area/topic.

## Forecasting
- Replace the simple bias average with a proper Kalman filter (as in NOAA's KFAN).
- Log predictions vs. outcomes, to score against validated EPA AQS data later
  (needed for any publication; AirNow data is preliminary).
