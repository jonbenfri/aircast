# Roadmap

Ideas and open items, roughly in priority order.

## Before open-sourcing
- Choose a license (MIT, Apache-2.0 or GPL-3.0) and add `LICENSE`.
- GitHub Action that runs `uv run python -m unittest` on every push.
- Short CONTRIBUTING note.

## Alerts
- A different notification icon for each alert type (watch / forecast / now / clear / error).
- Include AirNow's official forecast and any agency advisory in alerts (AirNow data-use guidelines).
- ~~NOAA HMS satellite smoke polygons~~ (done: smoke levels 0-4 and "incoming").
- Fire detections (NASA FIRMS, free MAP_KEY) to say which fire a plume comes from.
- Smoke forecasts (NOAA HRRR-Smoke) for near-surface smoke, not just the satellite column.

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
- A few areas: systemd template service (`aircast@.service`), one env file and topic per area.
- Many areas: one process serving a list of areas, sharing AirNow downloads between nearby areas.
  Watch Open-Meteo's free tier: roughly 18 calls per area per check, about 10k calls a day.
- Per-user location: either a fixed location the user sets, or the phone's location
  (e.g. OwnTracks posting to the server), mapped to the nearest area/topic.

## VOC from low-cost outdoor monitors
- AirGradient Open Air (Sensirion SGP41: VOC and NOx indices) and PurpleAir Flex/Zen (Bosch BME688).
- Indices are relative to each sensor's own recent baseline, so they catch changes well but drift
  back toward 100 during long smoke events: use as an early-warning layer, not a concentration.
- Reject local noise (a neighbor's dryer vent, grills): require several sensors to rise together
  (median, not mean), persistence over 2-3 readings, per-sensor robust z-scores, upwind sensors
  rising first along the back-trajectories, and agreement with HMS smoke / PM2.5 / ozone.

## Forecasting
- Replace the simple bias average with a proper Kalman filter (as in NOAA's KFAN).
- Log predictions vs. outcomes, to score against validated EPA AQS data later
  (needed for any publication; AirNow data is preliminary).
