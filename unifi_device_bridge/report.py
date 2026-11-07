"""Builds the inform JSON a UniFi switch would send."""
import time

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


def model_info(cfg):
    """(model, display name, port count, PoE ports) from the config."""
    model = cfg.get("model", MODEL["model"])
    if model not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}")
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
    neighbors = list(snapshot.neighbors)
    up = cfg.get("uplink_neighbor")   # for switches without LLDP: what the uplink port plugs into
    if up and uplink and not any(n.port == uplink for n in neighbors):
        neighbors.append(Neighbor(port=uplink, chassis_id=up["mac"].lower(), port_id=up.get("port", ""),
                                  name=up.get("name", "")))
    if neighbors:
        body["lldp_table"] = [
            {"chassis_id": n.chassis_id, "local_port_idx": n.port, "local_port_name": f"Port {n.port}",
             "port_id": n.port_id, "chassis_descr": n.name or n.description, "is_wired": True}
            for n in neighbors
        ]
    return body
