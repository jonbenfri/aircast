# aircast

Advanced air quality forecasting for one location: AQI threshold alerts that use
live monitors, hourly wind trajectories and satellite smoke plumes, pushed to
your phone through your own ntfy server.

Forecasts when the US AQI near you will go above a threshold (default **35**)
and sends a push notification to your phone through your own
[ntfy](https://ntfy.sh) server. It uses only the Python standard library.

## How it estimates

Each poll (every 15 min by default):

1. **Model forecast:** Open-Meteo's hourly US AQI (CAMS) for the past 24 h and the next 3 days.
2. **Bias correction:** with an AirNow key, the current observed AQI is compared
   to the model. A time-weighted average of recent `observed - model`
   errors is added to the forecast, fading with lead time: `bias * exp(-h/12)`.
   This is a crude version of the Kalman-filter bias correction NOAA uses
   for its air-quality forecasts (see *Prior work*).
3. **Wind, model upwind:** model AQI on a 50 km ring around you. For each hour,
   the ring point the wind blows *from* pulls the forecast toward its value.
4. **Back-trajectories:** for every hour in the next 24 h, the air that will
   arrive is traced backwards through the hourly forecast winds to where it is
   now. The transport wind is the mean of the 10 m and 850 hPa winds. Nothing is
   smoothed, so a 3-hour wind spike or a wind shift counts where it happens. Five
   wind scenarios are traced (forecast as-is, direction ±20°, speed ×0.7 and ×1.3),
   and their disagreement widens the uncertainty.
5. **Upwind monitors:** AirNow monitors (PM2.5, PM10, ozone, CO, NO₂) within
   ~155 mi near each trajectory's starting point tell what air is on its way,
   assuming it persists. This catches wildfire smoke the model misses.
6. **Uncertainty:** σ grows with lead time, with scenario spread and model/upwind
   disagreement, and in calm air (stagnation). `P(exceed) = 1 − Φ((35 − forecast)/σ)`.
7. **Upwind watch:** if any monitor (or the model where the trajectories start)
   reads above 35 and forecast winds bring that air to you within 24 h, you get
   a heads-up with an arrival time and how many wind scenarios agree.
8. **Smoke (separate state):** NOAA HMS satellite smoke plumes, the same gray
   polygons as on fire.airnow.gov. AQI monitors don't measure smoke VOCs, so this
   is tracked separately:

   | Level | Meaning |
   |---|---|
   | 0 | no plume within 200 mi (`AQI_SMOKE_FAR_MI`) |
   | 1 | plume within 200 mi |
   | 2 | plume within 100 mi |
   | 3 | plume within 50 mi |
   | 4 | plume overhead |
   | 5 | dense overhead: overlapping plumes (density weights ≥ 3) or a heavy plume |

   Each alert lists plumes overhead, within 50 mi and within 50–100 mi, the nearest
   plume's distance and direction, and a score (overhead density weights +
   0.5 × within 50 mi + 0.25 × within 100 mi + 0.125 × within 200 mi; light = 1, medium = 2, heavy = 3).
   It's flagged **incoming** when the back-trajectories start inside a plume
   within the next 24 h (`AQI_WATCH_HOURS`).
9. **Alerts.** Fields within a line are separated by `  ||  `, and each alert shows when
   its readings were measured. Example:

   ```
   Possible AQI rise: 72 upwind, 206 mi SW  ||  ~Rochester, NY
   Current AQI: 19  ||  1 PM ~ 1.5 hrs ago
   Upwind: AQI 72 at Youngstown State, 206 mi SW  ||  12 PM ~ 2.5 hrs ago
   Could arrive: ~Thu 8 AM
   Wind (mph): 0–6h S 7  ||  6–12h S 16, gusts 39  ||  12–24h S 16
   ```

   Set `AQI_PLACE` for the title label and `TZ` (e.g. `America/New_York`) so times are local.
   The alert types:
   - 👀 *Possible AQI rise: 45 upwind 99 mi S*: arrival time, scenario agreement,
     and the wind outlook at 0–6 h / 6–12 h / 12–24 h (mean, max and gusts in mph,
     so spikes stay visible)
   - ⚠️ *AQI may exceed 35 by Wed 14:00*, sent when any hour in the next 12 h has P ≥ 50%
   - 🚨 *AQI 52, above 35 now*, repeated at most every 6 h
   - ✅ *AQI back to 22* / *Upwind watch over*
   - 🌫️ *Smoke plume within 50 mi (47 mi SW)* / *Smoke plume overhead (light)* /
     *Dense smoke overhead* / *Smoke may arrive ~19:00*: sent when the level rises.
     Drops are silent until they've held for 3 h; *Smoke plumes cleared* when back to 0.
   - ❌ *monitor can't get data*, sent after 6 failed polls in a row (~3 h)
10. **Polling:** every 60 min when everything is clear, every 30 min when anything
    is brewing (any alert level, P ≥ 20%, AQI within 5 of the threshold, or any smoke).

Run `.venv/bin/python aircast.py status` to see the hourly table it uses to decide.

### What it doesn't see

The AQI covers only PM2.5, PM10, ozone, CO, NO₂ and SO₂. Smoke VOCs aren't
measured directly. They show up only as the ozone they form downwind.

## Setup on a DigitalOcean droplet (Ubuntu/Debian)

ntfy is served at `https://ntfy.example.com` through the nginx that
already hosts your site. Add a DNS A record `ntfy` pointing at the droplet first.

**1. Firewall** (if you use ufw):

```bash
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
```

**2. Install ntfy** from its apt repo:

```bash
sudo mkdir -p /etc/apt/keyrings
sudo curl -L -o /etc/apt/keyrings/ntfy.gpg https://archive.ntfy.sh/apt/keyring.gpg
sudo apt install apt-transport-https
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt stable main" \
    | sudo tee /etc/apt/sources.list.d/ntfy.list
sudo apt update
sudo apt install ntfy
sudo systemctl enable ntfy
sudo systemctl start ntfy
```

Use `arch=arm64` instead of `arch=amd64` on an ARM droplet.

**3. Configure ntfy and nginx:**

```bash
sudo cp deploy/ntfy-server.yml /etc/ntfy/server.yml
sudo cp deploy/nginx-ntfy.conf /etc/nginx/sites-available/ntfy
sudo ln -s /etc/nginx/sites-available/ntfy /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ntfy.example.com
sudo systemctl restart ntfy
curl -s https://ntfy.example.com/v1/health   # expect {"healthy":true}
```

**4. Create accounts.** Use least privilege: an admin for you, a read-only user for
the phone, and a write-only user whose token the monitor posts with.

```bash
sudo ntfy user add --role=admin admin
sudo ntfy user add phone && sudo ntfy access phone aqi read-only
sudo ntfy user add aqi-bot && sudo ntfy access aqi-bot aqi write-only
sudo ntfy token add aqi-bot
```

The last command prints `tk_...`, which goes in `NTFY_TOKEN`. Topic names are
case-sensitive: `aqi` and `AQI` are different topics.

**5. Phone.** Install **ntfy** from Google Play or F-Droid. Tap +, choose
*Use another server*, enter `https://ntfy.example.com`, then topic `aqi`, and log in
as `phone`. For a self-hosted server on Android, the app keeps its own connection
open ("instant delivery"). Let it ignore battery optimization so alerts aren't
delayed.

**6. Get the code onto the droplet** using a read-only deploy key:

```bash
sudo ssh-keygen -t ed25519 -f /root/.ssh/aircast_deploy -N "" -C "droplet aircast deploy"
sudo cat /root/.ssh/aircast_deploy.pub
```

Add that key under the repo's Settings → Deploy keys. Leave write access off.

```bash
sudo tee -a /root/.ssh/config <<'EOF'
Host github-aircast
    HostName github.com
    User git
    IdentityFile /root/.ssh/aircast_deploy
    IdentitiesOnly yes
EOF
sudo git clone git@github-aircast:YOUR-GITHUB-USER/aircast.git /opt/aircast
```

To update later:

```bash
cd /opt/aircast && sudo git pull && sudo env UV_PYTHON_INSTALL_DIR=/opt/python uv sync && sudo systemctl restart aircast
```

**7. Install the monitor.** It's a [uv](https://docs.astral.sh/uv/) project
pinned to Python 3.12 (`.python-version`). uv downloads that Python itself,
so this works even when the OS Python is old (Debian 9 has 3.5). There are no
third-party dependencies.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
cd /opt/aircast
sudo env UV_PYTHON_INSTALL_DIR=/opt/python uv sync
.venv/bin/python --version   # Python 3.12.x
```

`UV_PYTHON_INSTALL_DIR=/opt/python` keeps the interpreter out of `/root`,
where the `aqi` service user couldn't read it.

```bash
cd /opt/aircast
sudo useradd --system --no-create-home aqi
sudo mkdir -p /var/lib/aircast && sudo chown aqi:aqi /var/lib/aircast
sudo cp deploy/aircast.env.example /etc/aircast.env && sudo chmod 600 /etc/aircast.env
sudo nano /etc/aircast.env
```

In the env file, set `NTFY_TOKEN` and `AIRNOW_API_KEY` (a free key from
<https://docs.airnowapi.org/>).

```bash
sudo cp deploy/aircast.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aircast
```

**Notification icon (optional).** Alerts can show `deploy/icons/aqi.png` as
their large icon. Add this block inside the ntfy `server { ... }` in
`/etc/nginx/sites-available/ntfy`, in the `listen 443` one if certbot split them:

```nginx
location /icons/ {
    alias /opt/aircast/deploy/icons/;
}
```

Then run `sudo nginx -t && sudo systemctl reload nginx` and set
`NTFY_ICON=https://ntfy.example.com/icons/aqi.png` in `/etc/aircast.env`.

**8. Test:**

```bash
sudo -u aqi env $(sudo grep -v '^#' /etc/aircast.env | xargs -d '\n') /opt/aircast/.venv/bin/python /opt/aircast/aircast.py test-notify
sudo -u aqi env $(sudo grep -v '^#' /etc/aircast.env | xargs -d '\n') AQI_DB=/tmp/aircast-test.db /opt/aircast/.venv/bin/python /opt/aircast/aircast.py status
journalctl -u aircast -f
```

## Tests

```bash
uv run python -m unittest -v tests/test_monitor.py
```

The tests include the motivating case: smoke 40–80 km south, wind from the
south at 5 m/s, and a model that still says 20. The expected result is a
warning about 2 h ahead.

## Data sources and AirNow data-use guidelines

- **AirNow** (US EPA and the federal, state, local and tribal agencies that own
  the monitors): observed values are **preliminary**, not validated, and are
  passed through unchanged. Alerts don't carry a disclaimer; add one back if you
  share alerts with others.
- **Open-Meteo / CAMS:** model AQI and wind forecasts.
- This tool's probabilities and "may exceed" alerts are **personal estimates, not
  official forecasts or advisories**. Official ones come from the agencies via airnow.gov.

AirNow's guidelines matter if this goes beyond personal use:

- **Analyses or publications** must use validated data from EPA's Air Quality
  System (AQS), not AirNow. Credit the data-owning agencies and the AirNow
  program, and tell them about the publication.
- **Public displays** must use the official AQI categories and colors,
  label observations as preliminary, and show the agencies' current
  advisories unaltered.

## Attribution

- Air-quality and weather forecasts: [Open-Meteo](https://open-meteo.com/)
  (CC BY 4.0), using Copernicus Atmosphere Monitoring Service (CAMS) data.
- Smoke plumes: NOAA NESDIS Hazard Mapping System (HMS) smoke analysis.
- Observations: the U.S. EPA AirNow program and the state, local and tribal
  agencies that operate the monitors. Data are preliminary; see above.

## Prior work / ideas to borrow

- NOAA's operational PM2.5 forecasts apply Kalman-filter and analog-ensemble
  (KFAN) bias correction to CMAQ output. They report big gains overall but
  limited help during wildfire smoke, which is the gap the upwind-monitor
  step here targets.
- PM2.5-GNN (Wang et al., 2020) uses a graph neural network whose edges carry
  wind speed and direction between monitors. It's the learned version of the
  upwind weighting here.
- NOAA HYSPLIT does proper back-trajectories, and NOAA HMS smoke polygons
  (the gray areas on fire.airnow.gov) mark satellite-detected smoke.
