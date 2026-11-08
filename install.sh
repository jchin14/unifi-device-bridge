#!/bin/sh
# One-command install or update for unifi-device-bridge (Debian, Ubuntu, Raspberry Pi OS):
#   curl -fsSL https://raw.githubusercontent.com/jchin14/unifi-device-bridge/main/install.sh | sudo sh
# Run it again to update. Configs in /etc/unifi-device-bridge are never touched.
set -e
REPO=https://github.com/jchin14/unifi-device-bridge
DIR=/opt/unifi-device-bridge
[ "$(id -u)" = 0 ] || { echo "Run as root: curl -fsSL ... | sudo sh"; exit 1; }
command -v apt-get >/dev/null || { echo "Needs apt (Debian, Ubuntu, Raspberry Pi OS)."; exit 1; }

echo "Installing packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-cryptography snmp >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Needs Python 3.11 or newer."; exit 1; }

if [ -d "$DIR/.git" ]; then
  echo "Updating $DIR..."
  git -C "$DIR" pull -q --ff-only
else
  echo "Downloading to $DIR..."
  git clone -q "$REPO" "$DIR"
fi

cp "$DIR/systemd/unifi-device-bridge@.service" /etc/systemd/system/
mkdir -p /etc/unifi-device-bridge
systemctl daemon-reload

# Restart running devices so they pick up the update.
for f in /etc/unifi-device-bridge/*.toml; do
  [ -e "$f" ] || continue
  n=$(basename "$f" .toml)
  systemctl is-active -q "unifi-device-bridge@$n" && systemctl restart "unifi-device-bridge@$n" && echo "Restarted $n"
done

echo
echo "Installed $(cat "$DIR/VERSION")."
if ! ls /etc/unifi-device-bridge/*.toml >/dev/null 2>&1; then
  echo "Add a device (pick a short name, e.g. office):"
  echo "  sudo install -m 600 $DIR/config.example.toml /etc/unifi-device-bridge/office.toml"
  echo "  sudo nano /etc/unifi-device-bridge/office.toml"
  echo "  sudo systemctl enable --now unifi-device-bridge@office"
  echo "Then click Adopt in UniFi > Devices."
fi
