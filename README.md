# aqi-monitor

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
   the ring point the wind blows *from* and the travel time
   (`50 km / wind speed`) give an "advected" AQI, which pulls the forecast toward it.
4. **Wind, observed upwind:** AirNow monitors within 150 km (PM2.5,
   PM10, ozone, CO, NO₂). Uses the average wind vector from now to hour *h*, keeps
   monitors in the upwind cone, and weights each by alignment and by how closely its
   travel time matches *h*. If the plume persists, their readings show what
   arrives when. This catches wildfire smoke that the model misses.
5. **Uncertainty:** σ grows with lead time, with model/upwind disagreement, and
   in calm air (stagnation). `P(exceed) = 1 − Φ((35 − forecast)/σ)`.
6. **Upwind watch:** independent of the forecast math. If any AirNow monitor (or
   the model, at ~105 mi out) within 105 mi reads above 35, and the current wind
   or the next 6 h of forecast wind is blowing from it toward you (within ±45°),
   you get a heads-up with an estimated arrival time.
7. **Alerts:** a `clear → watch → forecast → now` state machine with hysteresis and
   reminders, so it doesn't spam you. Alert messages:
   - 👀 *Possible AQI rise: 45 upwind 99 mi S*: wind from S 5 m/s, could arrive
     ~18:00 if it persists, plus the 12 h wind outlook and "check the weather"
   - ⚠️ *AQI may exceed 35 by Wed 14:00*, sent when any hour in the next 12 h has P ≥ 50%
   - 🚨 *AQI 52, above 35 now*, repeated at most every 6 h
   - ✅ *AQI back to 22* / *Upwind watch over*, sent once it's ≤ 30, P < 20% and
     nothing bad is upwind
   - ❌ *monitor can't get data*, sent after 6 failed polls in a row (~3 h)
8. **Polling:** every 60 min when everything is clear, every 30 min when anything
   is brewing (any alert level, P ≥ 20%, or AQI within 5 of the threshold).

Run `python3 aqi_monitor.py status` to see the hourly table it uses to decide.

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
sudo ssh-keygen -t ed25519 -f /root/.ssh/aqi_deploy -N "" -C "droplet aqi-monitor deploy"
sudo cat /root/.ssh/aqi_deploy.pub
```

Add that key under the repo's Settings → Deploy keys. Leave write access off.

```bash
sudo tee -a /root/.ssh/config <<'EOF'
Host github-aqi
    HostName github.com
    User git
    IdentityFile /root/.ssh/aqi_deploy
    IdentitiesOnly yes
EOF
sudo git clone git@github-aqi:YOUR-GITHUB-USER/aqi-monitor.git /opt/aqi-monitor
```

To update later:

```bash
cd /opt/aqi-monitor && sudo git pull && sudo env UV_PYTHON_INSTALL_DIR=/opt/python uv sync && sudo systemctl restart aqi-monitor
```

**7. Install the monitor.** It's a [uv](https://docs.astral.sh/uv/) project
pinned to Python 3.12 (`.python-version`). uv downloads that Python itself,
so this works even when the OS Python is old (Debian 9 has 3.5). There are no
third-party dependencies.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
cd /opt/aqi-monitor
sudo env UV_PYTHON_INSTALL_DIR=/opt/python uv sync
.venv/bin/python --version   # Python 3.12.x
```

`UV_PYTHON_INSTALL_DIR=/opt/python` keeps the interpreter out of `/root`,
where the `aqi` service user couldn't read it.

```bash
cd /opt/aqi-monitor
sudo useradd --system --no-create-home aqi
sudo mkdir -p /var/lib/aqi-monitor && sudo chown aqi:aqi /var/lib/aqi-monitor
sudo cp deploy/aqi-monitor.env.example /etc/aqi-monitor.env && sudo chmod 600 /etc/aqi-monitor.env
sudo nano /etc/aqi-monitor.env
```

In the env file, set `NTFY_TOKEN` and `AIRNOW_API_KEY` (a free key from
<https://docs.airnowapi.org/>).

```bash
sudo cp deploy/aqi-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aqi-monitor
```

**Notification icon (optional).** Alerts can show `deploy/icons/aqi.png` as
their large icon. Add this block inside the ntfy `server { ... }` in
`/etc/nginx/sites-available/ntfy`, in the `listen 443` one if certbot split them:

```nginx
location /icons/ {
    alias /opt/aqi-monitor/deploy/icons/;
}
```

Then run `sudo nginx -t && sudo systemctl reload nginx` and set
`NTFY_ICON=https://ntfy.example.com/icons/aqi.png` in `/etc/aqi-monitor.env`.

**8. Test:**

```bash
sudo -u aqi env $(sudo grep -v '^#' /etc/aqi-monitor.env | xargs) /opt/aqi-monitor/.venv/bin/python /opt/aqi-monitor/aqi_monitor.py test-notify
sudo -u aqi env $(sudo grep -v '^#' /etc/aqi-monitor.env | xargs) AQI_DB=/tmp/aqi.db python3 /opt/aqi-monitor/aqi_monitor.py status
journalctl -u aqi-monitor -f
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
  the monitors): observed values are **preliminary**, not validated. They are
  passed through unchanged and labeled as preliminary in alerts.
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
