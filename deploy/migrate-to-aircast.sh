#!/bin/bash
# One-time move of an existing aqi-monitor install to the aircast names:
#   /opt/aqi-monitor      -> /opt/aircast
#   /etc/aqi-monitor.env  -> /etc/aircast.env
#   /var/lib/aqi-monitor  -> /var/lib/aircast   (keeps alert state and bias history)
#   aqi-monitor.service   -> aircast.service
# The 'aqi' service user, ntfy topic and accounts are unchanged.
# Run as root:  sudo bash /opt/aqi-monitor/deploy/migrate-to-aircast.sh
set -euo pipefail

move() {  # move SRC to DST unless DST already exists
    if [ -e "$1" ] && [ ! -e "$2" ]; then mv "$1" "$2"; echo "moved $1 -> $2"; fi
}

systemctl stop aqi-monitor 2>/dev/null || true
systemctl disable aqi-monitor 2>/dev/null || true

move /opt/aqi-monitor /opt/aircast
move /etc/aqi-monitor.env /etc/aircast.env
move /var/lib/aqi-monitor /var/lib/aircast
sed -i 's#/var/lib/aqi-monitor/#/var/lib/aircast/#' /etc/aircast.env

# The venv has absolute paths baked in; rebuild it in the new location.
rm -rf /opt/aircast/.venv
(cd /opt/aircast && UV_PYTHON_INSTALL_DIR=/opt/python /usr/local/bin/uv sync)

# Notification icon is served by nginx straight from the repo folder.
for f in /etc/nginx/sites-available/*; do
    if grep -q /opt/aqi-monitor/ "$f"; then
        sed -i 's#/opt/aqi-monitor/#/opt/aircast/#g' "$f"
        echo "updated $f"
    fi
done
nginx -t && systemctl reload nginx

rm -f /etc/systemd/system/aqi-monitor.service
cp /opt/aircast/deploy/aircast.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aircast

sleep 5
journalctl -u aircast -n 10 --no-pager
