# unifi-device-bridge

Show non-UniFi devices in UniFi Network. A Raspberry Pi (or any Linux box) reads the device and reports it to your controller as a UniFi device, so it can be adopted like the real thing.

The UniFi adoption process has been reverse engineered to add non-UniFi switches. More features are coming soon. Feel free to open a PR for your switch model, or send me one if you want me to try to reverse engineer it.

Today it supports managed switches, shown as a UniFi switch model (8 to 48 ports). It can also add [virtual devices](#virtual-devices): switches and access points that exist only to show up in UniFi.

## What you get

- Port up/down, speed, duplex and traffic
- Which clients are on which port (SNMP switches)
- PoE status (SNMP switches with POWER-ETHERNET-MIB)
- CPU and memory on the device page: real numbers from switches with SNMP HOST-RESOURCES-MIB. For switches that can't report them (Easy Smart), set fixed values with `cpu_percent` and `mem_percent`, shown as flat lines, or leave them unset to show nothing
- LLDP neighbours, and "Connected To" (uses the switch's real MAC)
- Optional control from UniFi: port on/off, PoE on/off (`manage_ports`) and the switch's IP (`manage_ip`)

## Drivers

- `snmp`: any managed switch with SNMP v1/v2c/v3
- `easysmart`: TP-Link Easy Smart (TL-SG105E, TL-SG108E, TL-SG108PE, TL-SG1016DE...) via the web UI. Ports and packet counts only, no per-port clients. Traffic in UniFi is an estimate (packets x `bytes_per_packet`, 700 by default), since the switch doesn't count bytes.
- `virtual`: no hardware at all, see [Virtual devices](#virtual-devices)
- `fake`: made-up ports, for testing

## Install

On Debian, Ubuntu or Raspberry Pi OS (needs Python 3.11+):

```sh
curl -fsSL https://raw.githubusercontent.com/jchin14/unifi-device-bridge/main/install.sh | sudo sh
```

Run the same command again to update. Your configs aren't touched and running devices restart.

## Add a device

Each device gets its own config file and service. Pick a short name, e.g. `office`:

```sh
sudo install -m 600 /opt/unifi-device-bridge/config.example.toml /etc/unifi-device-bridge/office.toml
sudo nano /etc/unifi-device-bridge/office.toml
sudo systemctl enable --now unifi-device-bridge@office
```

Then open UniFi > Devices and click **Adopt**. Repeat with another name for each extra switch.

## Main settings

- `inform_url`: your controller (UDM/UDR/UCG: the gateway IP, port 8080)
- `name`: shown in UniFi until you rename it there
- `uplink_port`: the port to your router or upstream switch, in the switch's own numbering. Any port works
- `model`: which UniFi switch to show, e.g. `US8`, `US8P60` (PoE on 5-8), `USL8LP` (PoE on 1-4), `US24`, `US48`
- `port_order = "reversed"`: switch port 8 shows as UniFi port 1. Lines up switches with PoE on 1-4 (like the TL-SG108PE) with the US8P60. UniFi labels ports by its own numbers, so rename them in UniFi if you want.

See `config.example.toml` for everything else.

## Virtual devices

Just for looks: switches and access points that adopt and show in UniFi with moving traffic, PoE draw, CPU and memory and a few days of uptime, but nothing real behind them. One config file holds as many as you like, each in its own `[[device]]` block, all run by one service:

```sh
sudo cp /opt/unifi-device-bridge/virtual.example.toml /etc/unifi-device-bridge/virtual.toml
sudo nano /etc/unifi-device-bridge/virtual.toml
sudo systemctl enable --now unifi-device-bridge@virtual
```

Then click **Adopt** on each one in UniFi > Devices.

```toml
inform_url = "http://192.168.1.1:8080/inform"
driver = "virtual"

[[device]]
name = "Core Switch"
model = "US24P250"
uplink_port = 1

[[device]]
name = "Living Room AP"
model = "U7-Pro-XGS"
```

Models:

| Type | Models |
| --- | --- |
| Switches | `US8`, `US8P60`, `USL8LP`, `USL16LP`, `US16P150`, `US24`, `US24P250`, `US48` |
| Wi-Fi 5 APs | `UAP-AC-Lite`, `UAP-AC-LR`, `UAP-AC-Pro`, `UWB-XG` (WiFi BaseStation XG) |
| Wi-Fi 6 APs | `U6-Lite`, `U6-LR`, `U6-Pro` |
| Wi-Fi 7 APs | `U7-Pro`, `U7-Pro-Max`, `U7-Pro-XG`, `U7-Pro-XGS`, `E7`, `E7-Audience` |

APs report their real radios (6 GHz on Wi-Fi 7, no 2.4 GHz on the E7 Audience), port speed (up to 10 GbE) and current firmware.

Optional settings, at the top level (all devices) or inside a `[[device]]` (just that one):

- `device_ip`: IP shown in UniFi (default: this machine's)
- `manage_ports = true`: port and PoE on/off in UniFi appear to work (remembered until restart). On in the example config.
- `[virtual] up_ports = [1, 2, 5]`: ports shown as connected (default: a random mix, the same on every restart)
- `[virtual] poe_watts = 4.5`: rough PoE draw per powered port
- `cpu_percent`, `mem_percent`: CPU and memory shown, as flat lines (the example config sets 10 and 45)
- APs: `virtual = { channels = [6, 149, 37] }`: one channel per radio, lowest band first (default: picked per device)

Good to know:

- No clients are shown, and no traffic is ever forwarded.
- Device names must be different. Renaming one in the config makes it a new device to adopt; renaming it in UniFi is fine.
- To adopt one again after removing it in UniFi: stop the service, delete its file in `/var/lib/unifi-device-bridge/virtual/` (named after the device, e.g. `core-switch.json`), start it.
- Virtual APs haven't been tried on a real controller yet. Virtual switches use the same report as real bridged switches.

## Changing the switch's IP from UniFi

Set `manage_ip = true` (Easy Smart only for now), then change the IP in UniFi > device > Settings > IP Configuration. The bridge applies it to the switch and follows it to the new IP.

- The bridge updates `host` in the config file to the new IP (the service runs as root for this; the rest of the system stays read-only).
- Use a static IP or a DHCP reservation. On DHCP, if the switch gets a new address the bridge can't find it.
- Change the IP in UniFi, not on the switch's own web page, or the bridge loses track of it. If that happens, update `host` in the config and restart the service.

## Port and PoE control from UniFi

Set `manage_ports = true`. Setting a port profile to Disabled turns the port off; setting PoE to Off turns PoE off.

- The uplink port is never disabled.
- Easy Smart: port on/off only. SNMP: port and PoE on/off; needs `write_community` (or SNMP v3 write access).

## VLANs from UniFi

On by default (Easy Smart only for now; set `manage_vlans = false` to turn it off). The bridge copies each port's native and tagged networks from UniFi to the switch's 802.1Q VLANs and PVIDs.

- By default every VLAN is tagged on every port (like UniFi's Default port profile, "Allow All"). Ports set to a specific profile in UniFi get just what that profile allows.

- Turns on 802.1Q VLAN mode on the switch the first time it's needed (this clears Port Based and MTU VLANs).
- VLANs not used in UniFi are deleted from the switch (except VLAN 1).
- The uplink port always stays untagged in VLAN 1, so the bridge can't lock itself out.
- Untested on real hardware so far: check the switch's VLAN page after the first change.

## Updates

Clicking Update in UniFi (or auto-update) is safe: nothing is downloaded or run. The bridge just reports the new version so UniFi clears the badge. Reboot requests are ignored.

## Commands

```sh
cd /opt/unifi-device-bridge
sudo python3 -m unifi_device_bridge -c /etc/unifi-device-bridge/office.toml --poll   # read the switch once
sudo python3 -m unifi_device_bridge -c /etc/unifi-device-bridge/virtual.toml --poll  # show every virtual device's ports
journalctl -u unifi-device-bridge@office -f
```

To adopt again after removing it in UniFi: stop the service, delete `/var/lib/unifi-device-bridge/office`, start it.

## Notes

- The bridge only talks outbound, it opens no ports.
- Tested on UniFi Network 10.6 (bridged switches).
- Easy Smart: the switch's web UI allows one login at a time. The bridge keeps one session open, and if you log into the web UI it backs off for 10 minutes (`webui_pause`, seconds) so it doesn't kick you out. UniFi shows the last reading meanwhile. A change you make in UniFi skips the wait and is applied straight away (which logs the web UI out).
- Turn off "Improve UniFi" (analytics) in your console settings. It can send your device list to Ubiquiti, which would show bridged devices with non-Ubiquiti MACs.

## Contributing

PRs for new switch models are welcome. A driver is one file in `unifi_device_bridge/drivers/` with a `poll()` that returns the ports (see `easysmart.py` for a web UI switch, `snmp.py` for SNMP). If your switch isn't supported and you can't write a driver, send me one and I'll try to reverse engineer it.

## License

Source-available under the [PolyForm Noncommercial 1.0.0](LICENSE) license. Commercial use: contact me.
