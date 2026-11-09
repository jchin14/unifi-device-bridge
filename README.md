# unifi-device-bridge

Show non-UniFi devices in UniFi Network. A Raspberry Pi (or any Linux box) reads the device and reports it to your controller as a UniFi device, so it can be adopted like the real thing.

The UniFi adoption process has been reverse engineered to add non-UniFi switches. More features are coming soon. Feel free to open a PR for your switch model, or send me one if you want me to try to reverse engineer it.

Today it supports managed switches, shown as a UniFi switch model (8 to 48 ports). It can also add [virtual devices](#virtual-devices): switches and access points that exist only to show up in UniFi.

## What you get

- Port up/down, speed, duplex and traffic
- Clients per port: SNMP switches from their MAC table, Easy Smart by a [client scan](#client-scan-easy-smart)
- PoE status (SNMP switches with POWER-ETHERNET-MIB)
- CPU and memory: real numbers over SNMP (HOST-RESOURCES-MIB), or fixed values from `cpu_percent` and `mem_percent`
- LLDP neighbours and "Connected To"
- [Control from UniFi](#control-from-unifi): ports, PoE, IP, VLANs, speed, mirroring, link aggregation, name and reboot

## Drivers

| Driver | Switches | Notes |
| --- | --- | --- |
| `snmp` | Any managed switch with SNMP v1/v2c/v3 | |
| `easysmart` | TP-Link Easy Smart (TL-SG105E, TL-SG108E, TL-SG108PE, TL-SG1016DE...) | Via the web UI. Traffic is estimated (packets x `bytes_per_packet`, default 700) |
| `virtual` | None, see [Virtual devices](#virtual-devices) | |
| `fake` | Made-up ports, for testing | |

Mercusys Easy Smart switches (e.g. MS108GS) use a near-identical web UI, so `easysmart` should work on them, but it's untested.

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
  - Easy Smart: the bridge finds the uplink itself (a short packet burst, read from the switch's counters) at start and at each client scan, and uses that port if the cable moved. `follow_uplink = false` keeps `uplink_port` and only warns. `check_uplink = false` turns the start check off
- `model`: which UniFi switch to show, e.g. `US8`, `US8P60` (PoE on 5-8), `USL8LP` (PoE on 1-4), `US24`, `US48`
- `port_order = "reversed"`: switch port 8 shows as UniFi port 1. Lines up switches with PoE on 1-4 (like the TL-SG108PE) with the US8P60. UniFi labels ports by its own numbers, so rename them in UniFi if you want.

See `config.example.toml` for everything else.

## Control from UniFi

All on by default. Each can be turned off in the config.

### Switch IP

On by default (Easy Smart only for now; `manage_ip = false` turns it off). Change the IP in UniFi > device > Settings > IP Configuration. The bridge applies it to the switch and follows it to the new IP.

- The bridge updates `host` in the config file to the new IP (the service runs as root for this; the rest of the system stays read-only).
- Use a static IP or a DHCP reservation. On DHCP, if the switch gets a new address the bridge can't find it.
- Change the IP in UniFi, not on the switch's own web page, or the bridge loses track of it. If that happens, update `host` in the config and restart the service.

### Ports and PoE

On by default (`manage_ports = false` turns it off). Setting a port profile to Disabled turns the port off; setting PoE to Off turns PoE off.

- The uplink port is never disabled.
- Easy Smart: port on/off only. SNMP: port and PoE on/off; needs `write_community` (or SNMP v3 write access).

### VLANs

On by default (Easy Smart only for now; set `manage_vlans = false` to turn it off). The bridge copies each port's native and tagged networks from UniFi to the switch's 802.1Q VLANs and PVIDs.

- By default every VLAN is tagged on every port (like UniFi's Default port profile, "Allow All"). Ports set to a specific profile in UniFi get just what that profile allows.
- Turns on 802.1Q VLAN mode on the switch the first time it's needed (this clears Port Based and MTU VLANs).
- VLANs not used in UniFi are deleted from the switch (except VLAN 1).
- The uplink port always stays untagged in VLAN 1, so the bridge can't lock itself out.
- Untested on real hardware so far: check the switch's VLAN page after the first change.

### Speed, mirroring and link aggregation

On by default (Easy Smart only for now; `manage_switching = false` turns it off). Set in UniFi > device > Ports.

- Link Speed: Auto, or a fixed 10/100 half/full or 1000 full.
- Port mirroring: one mirror port, never the uplink. While it's set, the client scan is skipped.
- Link aggregation: up to 2 groups of 2-4 ports. On 8-port models one group must be within ports 1-4 and the other within 5-8 (the switch's numbering).
- Removing the device in UniFi also turns these back to stock.

### Name

Renaming the device in UniFi renames the switch too (Easy Smart: its Device Description). On by default, `manage_name = false` turns it off.

- UniFi passes the name on as a hostname: letters, digits and hyphens only, so "Shed Switch" becomes "ShedSwitch".
- The switch allows 32 characters.

### Reboot, Locate and Update

- **Reboot** (Easy Smart): restarts the switch (about 40s), then runs a client scan. Other drivers ignore it.
- **Locate** (Easy Smart): the switch's LED can't be flashed from its web UI, so Locate runs a client scan instead.
- **Update** (or auto-update) is safe: nothing is downloaded or run. The bridge reports the new version so UniFi clears the badge.

## UniFi is in charge

UniFi's settings (ports, VLANs, name) are applied at start and re-applied every 5 minutes (`enforce_interval`, seconds; 0 turns it off). The switch's IP is only changed when you change it in UniFi.

- Settings on the switch's own web page are never read and may be overridden. Make changes in UniFi instead.
- To reset the switch to stock: remove (Forget) it in UniFi. The bridge turns VLANs off and every port on (`restore_on_forget`), then waits to be adopted again. The name is kept.
- Need a setting UniFi can't set, and the bridge keeps undoing it? [Open an issue](https://github.com/jchin14/unifi-device-bridge/issues) and say which setting.

## Where to plug in the bridge

SNMP switches: anywhere that can reach the switch. Everything works.

Easy Smart switches:

| | Not on the switch | On the switch |
| --- | --- | --- |
| Ports, speed, traffic | ✅ | ✅ |
| Control from UniFi | ✅ | ✅ |
| Uplink port found automatically | ✅ | ✅ |
| Clients per port | ❌ | ✅ by [client scan](#client-scan-easy-smart) |
| UniFi devices (e.g. APs) shown under this switch | ❌ | See below |

UniFi devices on the switch, with the bridge on the switch:

- None: nothing extra to do.
- One: the bridge tells it its port by LLDP, so UniFi shows it under this switch on the right port.
- Two or more: the switch floods LLDP to every port, so they'd all get the same port. Nothing is sent, and UniFi may show them under the upstream switch. They still show as clients on their ports.

## Client scan (Easy Smart)

Easy Smart switches have no MAC table, so the bridge finds clients by mirroring ports to its own port and noting who sends traffic.

- Needs the bridge plugged into the switch (not upstream of it). Otherwise it's skipped.
- Runs at start, every 12 hours (`scan_interval`, seconds; 0 = only when asked), after any settings change in UniFi, after Reboot, on Locate, when a port's link comes up (something plugged in; at most once per 10 minutes, `scan_on_link = false` turns it off), and when the switch restarts by itself (e.g. a power cut, seen as its packet counters resetting).
- Want fresh clients now? Press Locate, or change any setting in UniFi.
- Ports are grouped by binary code, so 7 ports take 3 rounds of about 10s each (`scan_seconds`).
- Each round also sends an ARP probe to every address on the bridge's subnet, so quiet devices answer.
- Clients behind the uplink belong to the upstream switch and aren't shown.
- Results stay until the next scan. A client that moved shows on its old port until then.
- A new device behind something already plugged in (e.g. a Wi-Fi client on an AP) doesn't change a port's link, so it shows at the next scan.
- Skipped if a port mirror is set in UniFi: the switch has only one.
- `scan_clients = false` turns it off.
- UniFi devices on the switch (e.g. APs): see [Where to plug in the bridge](#where-to-plug-in-the-bridge). `lldp_announce = false` turns off the LLDP sent to them.

## Flash wear (Easy Smart)

The switch saves every setting change to flash straight away, and flash wears out after many writes (often around 100,000 per block).

- A client scan is about 8 writes. Every 12 hours that's about 6,000 a year.
- Settings from UniFi only write when something differs. Reboot, polling and the uplink check write nothing.
- If you're worried, raise `scan_interval` or set `scan_clients = false`.

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

Want another UniFi model? PRs or [issues](https://github.com/jchin14/unifi-device-bridge/issues) to add more virtual UniFi hardware are welcome.

Optional settings, at the top level (all devices) or inside a `[[device]]` (just that one):

- `device_ip`: IP shown in UniFi (default: this machine's)
- `manage_ports`: port and PoE on/off in UniFi appear to work (remembered until restart). On by default.
- `[virtual] up_ports = [1, 2, 5]`: ports shown as connected (default: a random mix, the same on every restart)
- `[virtual] poe_watts = 4.5`: rough PoE draw per powered port
- `cpu_percent`, `mem_percent`: CPU and memory shown, as flat lines (the example config sets 10 and 45)
- APs: `virtual = { channels = [6, 149, 37] }`: one channel per radio, lowest band first (default: picked per device)

Good to know:

- No clients are shown, and no traffic is ever forwarded.
- Device names must be different. Renaming one in the config makes it a new device to adopt; renaming it in UniFi is fine.
- To adopt one again after removing it in UniFi: stop the service, delete its file in `/var/lib/unifi-device-bridge/virtual/` (named after the device, e.g. `core-switch.json`), start it.
- Virtual APs haven't been tried on a real controller yet. Virtual switches use the same report as real bridged switches.

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
- Easy Smart: press Locate in UniFi to run a client scan now (the LED can't flash). Changing any setting in UniFi also starts one.
- Turn off "Improve UniFi" (analytics) in your console settings. It can send your device list to Ubiquiti, which would show bridged devices with non-Ubiquiti MACs.

## Contributing

PRs for new switch models and more virtual UniFi hardware are welcome. A driver is one file in `unifi_device_bridge/drivers/` with a `poll()` that returns the ports (see `easysmart.py` for a web UI switch, `snmp.py` for SNMP). If your switch isn't supported and you can't write a driver, send me one and I'll try to reverse engineer it.

## Support

If you find this useful and can afford to, support is appreciated: [buy me a coffee](https://buymeacoffee.com/jchin14). Just getting started, the domain, hosting, Cloudflare Workers and Apple and Google developer accounts add up to a few hundred dollars. Your support helps me keep making tools that are helpful for everyone.

## Disclaimer

This software is provided as is, with no warranty. I'm not liable for any issues, problems, downtime or bricked devices from using it.

The risk is low:

- No firmware is ever written or flashed. Update in UniFi downloads nothing.
- It reads data from the switch and reports it to your UniFi controller.
- With control from UniFi on (the default), it changes switch settings through the switch's own web UI or SNMP, the same as you would by hand. Turn those off with the `manage_*` options and `scan_clients = false` for read-only use.

## License

Source-available under the [PolyForm Noncommercial 1.0.0](LICENSE) license. Commercial use: contact me.
