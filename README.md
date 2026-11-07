# unifi-device-bridge

Show non-UniFi devices in UniFi Network. A Raspberry Pi (or any Linux box) reads the device and reports it to your controller as a UniFi device, so it can be adopted like the real thing.

Today it supports managed switches, shown as a UniFi switch model (8 to 48 ports). Other device types may come later.

## What you get

- Port up/down, speed, duplex and traffic
- Which clients are on which port (SNMP switches)
- PoE status (SNMP switches with POWER-ETHERNET-MIB)
- LLDP neighbours, and "Connected To" (uses the switch's real MAC)
- Optional control from UniFi: port on/off, PoE on/off (`manage_ports`) and the switch's IP (`manage_ip`)

## Supported switches

- `snmp`: any managed switch with SNMP v1/v2c/v3
- `easysmart`: TP-Link Easy Smart (TL-SG105E, TL-SG108E, TL-SG108PE, TL-SG1016DE...) via the web UI. Ports and packet counts only, no per-port clients. Polling logs you out of the switch's web page.
- `fake`: made-up ports, for testing

## Install

Needs Python 3.11+, `python3-cryptography` and `snmp` (for the SNMP driver).

```sh
sudo apt install python3-cryptography snmp
sudo git clone https://github.com/jchin14/unifi-device-bridge /opt/unifi-device-bridge
sudo cp /opt/unifi-device-bridge/systemd/unifi-device-bridge@.service /etc/systemd/system/
sudo mkdir -p /etc/unifi-device-bridge
```

## Add a device

Each device gets its own config file and service. Pick a short name, e.g. `office`:

```sh
sudo cp /opt/unifi-device-bridge/config.example.toml /etc/unifi-device-bridge/office.toml
sudo chmod 600 /etc/unifi-device-bridge/office.toml
sudo nano /etc/unifi-device-bridge/office.toml
sudo systemctl enable --now unifi-device-bridge@office
```

Then open UniFi > Devices and click **Adopt**. Repeat with another name for each extra switch.

## Main settings

- `inform_url`: your controller (UDM/UDR/UCG: the gateway IP, port 8080)
- `name`: shown in UniFi until you rename it there
- `uplink_port`: the port to your router, in the switch's own numbering
- `model`: which UniFi switch to show, e.g. `US8`, `US8P60` (PoE on 5-8), `USL8LP` (PoE on 1-4), `US24`, `US48`
- `port_order = "reversed"`: switch port 8 shows as UniFi port 1. Lines up switches with PoE on 1-4 (like the TL-SG108PE) with the US8P60. UniFi labels ports by its own numbers, so rename them in UniFi if you want.

See `config.example.toml` for everything else.

## Changing the switch's IP from UniFi

Set `manage_ip = true` (Easy Smart only for now), then change the IP in UniFi > device > Settings > IP Configuration. The bridge applies it to the switch and follows it to the new IP.

- The bridge updates `host` in the config file to the new IP (the service runs as root for this; the rest of the system stays read-only).
- Use a static IP or a DHCP reservation. On DHCP, if the switch gets a new address the bridge can't find it.
- Change the IP in UniFi, not on the switch's own web page, or the bridge loses track of it. If that happens, update `host` in the config and restart the service.

## Port and PoE control from UniFi

Set `manage_ports = true`. Setting a port profile to Disabled turns the port off; setting PoE to Off turns PoE off.

- The uplink port is never disabled.
- Easy Smart: port on/off only. SNMP: port and PoE on/off; needs `write_community` (or SNMP v3 write access).
- VLANs aren't applied yet.

## Updates

Clicking Update in UniFi (or auto-update) is safe: nothing is downloaded or run. The bridge just reports the new version so UniFi clears the badge. Reboot requests are ignored.

## Commands

```sh
cd /opt/unifi-device-bridge
sudo python3 -m unifi_device_bridge -c /etc/unifi-device-bridge/office.toml --poll   # read the switch once
journalctl -u unifi-device-bridge@office -f
```

To adopt again after removing it in UniFi: stop the service, delete `/var/lib/unifi-device-bridge/office`, start it.

## Notes

- The bridge only talks outbound, it opens no ports.
- Tested on UniFi Network 10.6.

## License

Source-available under the [PolyForm Noncommercial 1.0.0](LICENSE) license. Commercial use: contact me.
