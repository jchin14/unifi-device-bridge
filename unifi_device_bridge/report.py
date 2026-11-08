"""Builds the inform JSON a UniFi switch would send."""
import time

from . import stats
from .model import Neighbor

MODEL = {"model": "US8", "model_display": "UniFi Switch 8", "version": "7.5.15.17146", "required_version": "5.4.0"}
# Models we can pretend to be: display name, RJ45 port count, ports with PoE.
MODELS = {
    "US8": ("UniFi Switch 8", 8, ()),
    "US8P60": ("UniFi Switch 8 POE-60W", 8, range(5, 9)),
    "USL8LP": ("UniFi Switch Lite 8 PoE", 8, range(1, 5)),
    "USL16LP": ("UniFi Switch Lite 16 PoE", 16, range(1, 9)),
    "US16P150": ("UniFi Switch 16 POE-150W", 16, range(1, 17)),
    "US24": ("UniFi Switch 24", 24, ()),
    "US24P250": ("UniFi Switch 24 POE-250W", 24, range(1, 25)),
    "US48": ("UniFi Switch 48", 48, ()),
}
# Access points (virtual only, nothing real behind them), from Ubiquiti's device catalog
# (static.ui.com/fingerprint/ui/public.json) and current firmware (fw-update.ubnt.com).
# Inform model code: display name, other names accepted in the config, Wi-Fi generation, firmware,
# Ethernet speed (Mb/s), radios as (band, max tx power dBm). Bands: ng 2.4, na 5, 6e 6 GHz.
FW_AC, FW_U6, FW_U7 = "6.8.2.15592", "6.7.57.15670", "8.7.11.19419"
AP_MODELS = {
    "U7LT": ("Access Point AC Lite", ("UAP-AC-Lite",), 5, FW_AC, 1000, (("ng", 20), ("na", 20))),
    "U7LR": ("Access Point AC Long-Range", ("UAP-AC-LR",), 5, FW_AC, 1000, (("ng", 24), ("na", 22))),
    "U7PG2": ("Access Point AC Pro", ("UAP-AC-Pro",), 5, FW_AC, 1000, (("ng", 22), ("na", 22))),
    "UXSDM": ("WiFi BaseStation XG", ("UWB-XG",), 5, FW_AC, 1000, (("na", 25),)),
    "UAL6": ("Access Point U6 Lite", ("U6-Lite",), 6, FW_U6, 1000, (("ng", 20), ("na", 25))),
    "UALR6v2": ("Access Point U6 Long-Range", ("U6-LR", "UALR6"), 6, FW_U6, 1000, (("ng", 20), ("na", 25))),
    "UAP6MP": ("Access Point U6 Pro", ("U6-Pro",), 6, FW_AC, 1000, (("ng", 25), ("na", 25))),
    "U7PRO": ("Access Point U7 Pro", ("U7-Pro",), 7, FW_U7, 2500, (("ng", 22), ("na", 26), ("6e", 26))),
    "U7PROMAX": ("Access Point U7 Pro Max", ("U7-Pro-Max",), 7, FW_U7, 2500, (("ng", 22), ("na", 26), ("6e", 26))),
    "UAPA6A9": ("Access Point U7 Pro XG", ("U7-Pro-XG",), 7, FW_U7, 10000, (("ng", 22), ("na", 26), ("6e", 23))),
    "UAPA6A4": ("Access Point U7 Pro XGS", ("U7-Pro-XGS",), 7, FW_U7, 10000, (("ng", 23), ("na", 29), ("6e", 23))),
    "UAPA697": ("Access Point E7", ("E7",), 7, FW_U7, 10000, (("ng", 22), ("na", 29), ("6e", 29))),
    "UAPA699": ("Access Point E7 Audience", ("E7-Audience",), 7, FW_U7, 10000, (("na", 30), ("6e", 30))),
}
AP_ALIASES = {name.lower(): code for code, (_d, names, *_r) in AP_MODELS.items() for name in (code, *names)}
# Channel width and the channels a virtual AP picks from, per band.
BANDS = {"ng": ("20", (1, 6, 11)), "na": ("80", (36, 44, 149, 157)), "6e": ("160", (37, 69, 101, 133))}


def ap_code(cfg):
    """Inform model code for an AP model in the config (code or product name), or None for a switch."""
    return AP_ALIASES.get(str(cfg.get("model", "")).lower())


def is_ap(cfg):
    return ap_code(cfg) is not None


def model_info(cfg):
    """(model, display name, port count, PoE ports) from the config."""
    model = cfg.get("model", MODEL["model"])
    if is_ap(cfg):
        code = ap_code(cfg)
        return code, AP_MODELS[code][0], 1, ()
    if model not in MODELS:
        names = [n for _d, aliases, *_r in AP_MODELS.values() for n in aliases]
        raise ValueError(f"model must be one of {', '.join([*MODELS, *names])}")
    display, count, poe = MODELS[model]
    return model, display, count, tuple(poe)


def port_entry(port, uplink, can_see_clients, poe_ports=(), first_seen=None, now=0):
    e = {
        "port_idx": port.index, "name": port.name or f"Port {port.index}", "media": "GE",
        "up": port.up, "enable": port.enabled, "is_uplink": port.index == uplink,
        "speed": port.speed if port.up and port.speed else 0,
        "full_duplex": bool(port.full_duplex) if port.up else False,
    }
    for key, val in (("rx_bytes", port.rx_bytes), ("tx_bytes", port.tx_bytes),
                     ("rx_packets", port.rx_packets), ("tx_packets", port.tx_packets),
                     ("rx_errors", port.rx_errors), ("tx_errors", port.tx_errors)):
        if val is not None:
            e[key] = val
    if port.index in poe_ports:
        e.update({"port_poe": True, "poe_caps": 7, "poe_mode": "off" if port.poe_enabled is False else "auto",
                  "poe_enable": port.poe_enabled is not False})
        if port.poe_good is not None:
            e["poe_good"] = port.poe_good
        if port.poe_power is not None:
            e["poe_power"] = f"{port.poe_power:.2f}"
    if can_see_clients:
        # Clients behind the uplink belong to the upstream switch, so don't claim them here.
        macs = [] if port.index == uplink or not port.up else port.macs
        first_seen = first_seen or {}
        e["mac_table"] = [{"mac": m, "age": 0, "static": False,
                           "uptime": max(0, now - first_seen.get((port.index, m), now)), **({"vlan": v} if v else {})}
                          for m, v in macs]
    return e


def build(snapshot, state, cfg, inform_url, started, first_seen=None):
    now = int(time.time())
    uplink = cfg.get("uplink_port", 0)
    model, display, _count, poe_ports = model_info(cfg)
    body = {
        **MODEL,
        "model": model,
        "model_display": display,
        "version": cfg.get("firmware_version") or state.get("firmware") or MODEL["version"],
        "mac": state["mac"],
        "serial": state["mac"].replace(":", "").upper(),
        "hostname": cfg.get("name", "unifi-device-bridge"),
        "ip": cfg["device_ip"],
        "inform_url": inform_url,
        "default": not state.get("authkey"),
        "state": 2 if state.get("authkey") else 0,
        "cfgversion": state.get("cfgversion", ""),
        "uptime": snapshot.uptime if snapshot.uptime is not None else now - int(started),
        "time": now,
        "discovery_response": False,
        "port_table": [port_entry(p, uplink, snapshot.can_see_clients, poe_ports, first_seen, now) for p in snapshot.ports],
    }
    if snapshot.cpu is not None or snapshot.mem is not None:
        body["system-stats"] = {**({"cpu": str(snapshot.cpu)} if snapshot.cpu is not None else {}),
                                **({"mem": str(snapshot.mem)} if snapshot.mem is not None else {}),
                                "uptime": str(body["uptime"])}
        body["sys_stats"] = stats.sys_stats(snapshot)
    if is_ap(cfg):
        ap_fields(body, snapshot, cfg, state)
        return body
    neighbors = list(snapshot.neighbors)
    up = cfg.get("uplink_neighbor")   # for switches without LLDP: what the uplink port plugs into
    if up and uplink and not any(n.port == uplink for n in neighbors):
        neighbors.append(Neighbor(port=uplink, chassis_id=up["mac"].lower(), port_id=up.get("port", ""),
                                  name=up.get("name", "")))
    if up and uplink:   # tell UniFi directly what the uplink is, as UniFi switches do
        remote = "".join(c for c in str(up.get("port", "")) if c.isdigit())
        local = next((e["port_idx"] for e in body["port_table"] if e["is_uplink"]), uplink)
        # The controller reads "uplink" as an interface name and looks it up in if_table; the
        # matching entry's uplink_* fields end up in the device's last_uplink ("Connected To").
        body["uplink"] = "eth0"
        body["if_table"] = [{"name": "eth0", "mac": state["mac"], "ip": body["ip"], "up": True, "type": "wire",
                             "port_idx": local, "uplink_mac": up["mac"].lower(),
                             "uplink_device_name": up.get("name", ""),
                             **({"uplink_remote_port": int(remote)} if remote else {})}]
    if neighbors:
        body["lldp_table"] = [
            {"chassis_id": n.chassis_id, "local_port_idx": n.port, "local_port_name": f"Port {n.port}",
             "port_id": n.port_id, "chassis_descr": n.name or n.description, "is_wired": True}
            for n in neighbors
        ]
    return body


def ap_fields(body, snapshot, cfg, state):
    """Turn the report into an access point: one Ethernet port, its radios, no clients."""
    _display, _names, gen, firmware, eth_speed, radios = AP_MODELS[body["model"]]
    if not (cfg.get("firmware_version") or state.get("firmware")):
        body["version"] = firmware
    port = snapshot.ports[0] if snapshot.ports else None
    eth = {"name": "eth0", "mac": state["mac"], "up": True, "speed": eth_speed, "full_duplex": True,
           "rx_bytes": (port.rx_bytes if port else 0) or 0, "tx_bytes": (port.tx_bytes if port else 0) or 0}
    body["port_table"] = body["port_table"][:1]
    for e in body["port_table"]:
        e.update({"is_uplink": True, "speed": eth_speed if e["up"] else 0,
                  "media": "10GE" if eth_speed >= 10000 else "2P5GE" if eth_speed >= 2500 else "GE"})
        e.pop("mac_table", None)
    body["ethernet_table"] = [{"mac": state["mac"], "num_port": 1, "name": "eth0"}]
    body["if_table"] = [{**eth, "ip": body["ip"], "netmask": "255.255.255.0"}]
    body["uplink"] = {**eth, "type": "wire", "port_idx": 1}
    wanted = list(cfg.get("virtual", {}).get("channels") or [])   # one per radio, in the model's band order
    n = int(state["mac"].replace(":", ""), 16)   # stable per device so each AP picks its own channels
    table = []
    for i, (band, power) in enumerate(radios):
        ht, choices = BANDS[band]
        channel = wanted[i] if i < len(wanted) else choices[(n >> (4 * i)) % len(choices)]
        table.append((f"wifi{i}", band, channel, ht, power))
    body["radio_table"] = [
        {"name": name, "radio": band, "channel": c, "ht": ht, "min_txpower": 6, "max_txpower": power,
         "nss": 4 if body["model"] in ("UXSDM", "UAPA699", "UAPA697") else 2, "builtin_antenna": True,
         "builtin_ant_gain": 3, "is_11ac": band != "ng", "is_11ax": gen >= 6, "is_11be": gen >= 7,
         "has_dfs": band == "na", "has_fccdfs": band == "na"}
        for name, band, c, ht, power in table]
    body["radio_table_stats"] = [
        {"name": name, "radio": band, "channel": c, "tx_power": power, "num_sta": 0, "state": "RUN",
         "cu_total": 8 + (c % 7), "cu_self_rx": 2, "cu_self_tx": 1, "satisfaction": 100}
        for name, band, c, _ht, power in table]
    body["vap_table"] = []
    body["num_sta"] = 0
