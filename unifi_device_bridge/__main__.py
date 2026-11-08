"""unifi-device-bridge: show non-UniFi devices in UniFi Network: managed switches, or virtual switches and APs."""
import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from . import inform, report, stats
from .drivers import DriverError, make_driver

VERSION = "0.26.2"  # kept in sync with VERSION by scripts/bump.sh
log = logging.getLogger("bridge")


class State:
    """Identity and adoption key, kept across restarts."""

    def __init__(self, path):
        self.path = path
        self.network = None       # IP settings from the last controller config, applied once
        self.ports = None         # port settings from the last controller config, applied once
        self.vlans = None         # VLANs from the last controller config, applied once
        try:
            with open(path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            mac = bytearray(os.urandom(6))
            mac[0] = (mac[0] & 0xFC) | 0x02          # locally administered, unicast
            self.data = {"mac": ":".join(f"{b:02x}" for b in mac)}
            self.save()
            log.info("new device identity %s", self.data["mac"])

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def reset(self):
        self.data = {k: v for k, v in self.data.items() if k in ("mac", "host")}
        self.save()


def state_file(cfg):
    """One state file per device. systemd sets STATE_DIRECTORY per service instance."""
    return cfg.get("state_file") or os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/unifi-device-bridge"), "state.json")


def device_configs(cfg):
    """A config with [[device]] entries runs several devices in one process (handy for virtual ones).
    Each entry overrides the top-level settings and gets its own state file, named after the device."""
    entries = cfg.get("device")
    if not entries:
        return [cfg]
    base = {k: v for k, v in cfg.items() if k != "device"}
    state_dir = os.path.dirname(state_file(base))
    out, slugs = [], set()
    for i, entry in enumerate(entries, 1):
        c = {**base, **entry}
        c.setdefault("name", f"Device {i}")
        slug = re.sub(r"[^a-z0-9]+", "-", c["name"].lower()).strip("-") or str(i)
        if slug in slugs:
            raise ValueError(f"two devices are named {c['name']!r}: give each a different name")
        slugs.add(slug)
        c["state_file"] = entry.get("state_file") or os.path.join(state_dir, f"{slug}.json")
        out.append(c)
    return out


def run_all(cfgs, once=False):
    """Run each device in its own thread. One device failing doesn't stop the others."""
    if len(cfgs) == 1:
        return run(cfgs[0], once=once)
    results = {}

    def one(c):
        try:
            results[c["name"]] = run(c, once=once)
        except Exception as e:   # bad config for one device: keep the rest going
            log.error("%s stopped: %s", c["name"], e)
            results[c["name"]] = f"error: {e}"

    threads = [threading.Thread(target=one, args=(c,), name=c["name"], daemon=True) for c in cfgs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return "\n".join(f"{name}: {status}" for name, status in results.items())


def inform_url_by_ip(url):
    """UniFi 10.x rejects adoption when the reported inform_url is a hostname."""
    p = urllib.parse.urlsplit(url)
    ip = socket.getaddrinfo(p.hostname, None, socket.AF_INET)[0][4][0]
    return urllib.parse.urlunsplit(p._replace(netloc=f"{ip}:{p.port}" if p.port else ip))


def local_ip(url):
    p = urllib.parse.urlsplit(url)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((p.hostname, p.port or 8080))
        return s.getsockname()[0]


def network_from_system_cfg(text):
    """UniFi system_cfg -> {"dhcp", "ip", "netmask", "gateway"}, or None if it has no IP settings."""
    kv = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))
    if kv.get("dhcpc.1.status") == "enabled":
        return {"dhcp": True}
    ip = kv.get("netconf.1.ip")
    if not ip or ip == "0.0.0.0" or not kv.get("netconf.1.netmask"):
        return None
    return {"dhcp": False, "ip": ip, "netmask": kv["netconf.1.netmask"], "gateway": kv.get("route.1.gateway", "")}


def ports_from_system_cfg(text):
    """UniFi system_cfg -> {unifi port: {"enabled": bool, "poe": bool or None}}."""
    kv = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))
    ports = {}
    for key, val in kv.items():
        m = re.fullmatch(r"switch\.port\.(\d+)\.(\w+)", key)
        if not m:
            continue
        p = ports.setdefault(int(m.group(1)), {"enabled": True, "poe": None})
        if m.group(2) in ("status", "opmode") and val == "disabled":
            p["enabled"] = False
        elif m.group(2) == "poe":
            p["poe"] = val != "off"
    return ports


def vlans_from_system_cfg(text):
    """UniFi system_cfg -> {"vlans": {vid: {"tagged": {port}, "untagged": {port}}}, "pvid": {port: vid}},
    UniFi port numbers, or None if it has no VLANs. Keys: switch.vlan.<i>.id, switch.vlan.<i>.mode (the VLAN's
    default for ports not listed; unset means tagged, so every VLAN reaches every port) and switch.vlan.<i>.port.<p>.mode = tagged/untagged/exclude."""
    kv = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))
    ids, modes, per_port, ports = {}, {}, {}, set()
    for key, val in kv.items():
        if m := re.fullmatch(r"switch\.vlan\.(\d+)\.id", key):
            ids[int(m.group(1))] = int(val)
        elif m := re.fullmatch(r"switch\.vlan\.(\d+)\.mode", key):
            modes[int(m.group(1))] = val
        elif m := re.fullmatch(r"switch\.vlan\.(\d+)\.port\.(\d+)\.mode", key):
            per_port.setdefault(int(m.group(1)), {})[int(m.group(2))] = val
        elif m := re.fullmatch(r"switch\.port\.(\d+)\.\w+", key):
            ports.add(int(m.group(1)))
    if not ids:
        return None
    vlans, pvid = {}, {}
    for i, vid in ids.items():
        if not 1 <= vid <= 4094:
            continue
        v = vlans.setdefault(vid, {"tagged": set(), "untagged": set()})
        for p in ports:
            mode = per_port.get(i, {}).get(p, modes.get(i, "tagged"))
            if mode == "untagged":
                v["untagged"].add(p)
                pvid[p] = vid
            elif mode == "tagged":
                v["tagged"].add(p)
    return {"vlans": vlans, "pvid": pvid}


def apply_vlans(driver, cfg, wanted, ports, reverse):
    """Make the switch's 802.1Q VLANs match UniFi's port profiles / networks."""
    if not (wanted and cfg.get("manage_vlans", True) and hasattr(driver, "set_vlans")):
        return
    sw = (lambda p: ports + 1 - p) if reverse else (lambda p: p)
    want = {"vlans": {vid: {k: {sw(p) for p in v[k] if p <= ports} for k in ("tagged", "untagged")}
                      for vid, v in wanted["vlans"].items()},
            "pvid": {sw(p): vid for p, vid in wanted["pvid"].items() if p <= ports}}
    # Never cut the bridge off: the uplink stays an untagged member of VLAN 1 (management).
    up = cfg.get("uplink_port")
    if up:
        up = sw(up)
        v1 = want["vlans"].setdefault(1, {"tagged": set(), "untagged": set()})
        if up not in v1["untagged"]:
            log.warning("keeping switch port %s untagged in VLAN 1: it's the uplink", up)
            for v in want["vlans"].values():
                v["untagged"].discard(up)
            v1["tagged"].discard(up)
            v1["untagged"].add(up)
            want["pvid"][up] = 1
    log.info("applying VLANs %s", sorted(want["vlans"]))
    driver.set_vlans(want)


def apply_ports(driver, cfg, wanted, snap, ports, reverse):
    """Turn ports and PoE on/off to match UniFi (port profile "Disabled", PoE "Off")."""
    if not (wanted and cfg.get("manage_ports")):
        return
    by_index = {p.index: p for p in snap.ports}
    for n, want in sorted(wanted.items()):
        port = by_index.get(n)
        if port is None:
            continue
        switch_port = ports + 1 - n if reverse else n
        if n == cfg.get("uplink_port") and not want["enabled"]:
            log.warning("not disabling port %s: it's the uplink", switch_port)
            continue
        if want["enabled"] != port.enabled and hasattr(driver, "set_port_enabled"):
            log.info("switch port %s: %s", switch_port, "enabling" if want["enabled"] else "disabling")
            driver.set_port_enabled(switch_port, want["enabled"])
        if (want["poe"] is not None and port.poe_enabled is not None and want["poe"] != port.poe_enabled
                and hasattr(driver, "set_poe")):
            log.info("switch port %s: PoE %s", switch_port, "on" if want["poe"] else "off")
            driver.set_poe(switch_port, want["poe"])


def apply_network(driver, cfg, wanted, state):
    """Make the switch's IP match UniFi's device settings (Settings > IP Configuration)."""
    if not (wanted and cfg.get("manage_ip") and hasattr(driver, "set_ip")):
        return
    current = driver.get_ip()
    if wanted["dhcp"] == current["dhcp"] and (wanted["dhcp"] or all(
            wanted[k] == current[k] for k in ("ip", "netmask", "gateway"))):
        return
    log.warning("changing switch IP settings to %s (was %s)", wanted, current)
    driver.set_ip(wanted)
    if not wanted["dhcp"]:
        use_host(driver, cfg, wanted["ip"])
        try:
            if save_host(cfg["_path"], cfg.get("driver", "snmp"), wanted["ip"]):
                log.info("updated host in %s", cfg["_path"])
                state.data.pop("host", None)
            else:
                raise OSError("no host line found")
        except (OSError, KeyError) as e:
            # Can't edit the config: remember the IP in state instead, it wins over the config.
            log.warning("couldn't update the config (%s): keeping the new IP in the state file", e)
            state.data["host"] = wanted["ip"]
        state.save()
    else:
        log.warning("switch is now on DHCP: if its IP changes, update host in the config")


def save_host(path, section, host):
    """Rewrite host = "..." in the driver's [section] of the config file, keeping everything else."""
    with open(path) as f:
        lines = f.read().splitlines(keepends=True)
    current = None
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            current = m.group(1).strip()
        elif current == section and re.match(r"\s*host\s*=", line):
            lines[i] = f'host = "{host}"\n'
            break
    else:
        return False
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.writelines(lines)
    os.replace(tmp, path)
    return True


def use_host(driver, cfg, host):
    driver.set_host(host)
    cfg["device_ip"] = host


def handle(reply, state):
    """Apply a controller reply. Returns seconds until the next inform."""
    kind = reply.get("_type")
    if kind == "setparam":
        mgmt = dict(l.split("=", 1) for l in reply.get("mgmt_cfg", "").splitlines() if "=" in l)
        if mgmt.get("authkey"):
            if not state.data.get("authkey"):
                log.info("adopted: got a device key from the controller")
            state.data["authkey"] = mgmt["authkey"]
        if "use_aes_gcm" in mgmt:
            state.data["gcm"] = mgmt["use_aes_gcm"] in ("true", "1")
        if reply.get("system_cfg"):
            state.network = network_from_system_cfg(reply["system_cfg"])
            state.ports = ports_from_system_cfg(reply["system_cfg"])
            state.vlans = vlans_from_system_cfg(reply["system_cfg"])
        cfg = reply.get("cfgversion") or mgmt.get("cfgversion")
        if cfg:
            state.data["cfgversion"] = cfg
        state.save()
        return 1
    if kind == "setdefault":
        log.warning("controller forgot this device: resetting to unadopted")
        state.reset()
        return 1
    if kind == "noop":
        return 1 if reply.get("immediate") else max(1, min(int(reply.get("interval", 10)), 300))
    if kind == "upgrade":
        # Never download or run anything. Just report the new version so UniFi
        # sees the "upgrade" finish and stops offering it.
        version = str(reply.get("version", ""))
        if re.fullmatch(r"\d+(\.\d+){2,4}", version):
            log.info("controller asked to upgrade to %s: ignored, now reporting that version", version)
            state.data["firmware"] = version
            state.save()
        return 1
    if kind in ("cmd", "reboot"):
        log.info("ignoring controller %s (read-only bridge)", kind)
    return 10


def send(cfg, state, body):
    key = bytes.fromhex(state.data["authkey"]) if state.data.get("authkey") else inform.DEFAULT_KEY
    packet = inform.encode(body, state.data["mac"], key, gcm=bool(state.data.get("authkey") and state.data.get("gcm")))
    req = urllib.request.Request(cfg["inform_url"], data=packet, headers={
        "Content-Type": "application/x-binary", "User-Agent": "AirControl Agent v1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read()
    return inform.decode(data, key)[1]


def reorder(snap, ports, reverse):
    """Number ports backwards (switch port 8 shows as UniFi port 1), e.g. to line PoE ports up."""
    if reverse:
        for p in snap.ports:
            p.index = ports + 1 - p.index
        for n in snap.neighbors:
            n.port = ports + 1 - n.port
        snap.ports.sort(key=lambda p: p.index)
    return snap


def run(cfg, once=False):
    state = State(state_file(cfg))
    ports = int(cfg.get("ports") or report.model_info(cfg)[2])
    driver = make_driver(cfg, ports)
    reverse = cfg.get("port_order", "normal") == "reversed"
    if reverse and cfg.get("uplink_port"):
        cfg["uplink_port"] = ports + 1 - cfg["uplink_port"]   # config uses the switch's own numbering
    # Show the switch's own IP in UniFi when we know it, otherwise this machine's.
    host = cfg.get(cfg.get("driver", "snmp"), {}).get("host", "")
    if host and not host.startswith("127."):
        cfg.setdefault("device_ip", host)
    cfg.setdefault("device_ip", local_ip(cfg["inform_url"]))
    if state.data.get("host") and hasattr(driver, "set_host"):
        log.info("using switch IP %s set from UniFi", state.data["host"])
        use_host(driver, cfg, state.data["host"])
    started = time.time()
    delay, last_status = 10, None
    last_vlans = None
    first_seen = {}   # (port, mac) -> when we first saw it, for client uptime
    while True:
        try:
            snap = stats.fill(reorder(driver.poll(), ports, reverse), cfg)
            # Use the switch's real MAC so UniFi can see where it's plugged in ("Connected To").
            mac = (cfg.get("device_mac") or (snap.mac if cfg.get("use_switch_mac", True) else None) or "").lower()
            if mac and mac != state.data["mac"]:
                log.warning("device MAC is now %s (was %s): adopt it again in UniFi", mac, state.data["mac"])
                state.data = {"mac": mac, **({"host": state.data["host"]} if "host" in state.data else {})}
                state.save()
            now = int(time.time())
            seen = {(p.index, m) for p in snap.ports for m, _ in p.macs}
            first_seen = {k: first_seen.get(k, now) for k in seen}
            body = report.build(snap, state.data, cfg, inform_url_by_ip(cfg["inform_url"]), started, first_seen)
            before = state.data.get("cfgversion")
            reply = send(cfg, state, body)
            delay = handle(reply, state)
            if state.data.get("cfgversion") != before and hasattr(driver, "resume"):
                driver.resume()   # you just changed something in UniFi: don't wait out the web UI pause
            if state.ports:
                wanted, state.ports = state.ports, None
                apply_ports(driver, cfg, wanted, snap, ports, reverse)
            if state.vlans:
                wanted, state.vlans = state.vlans, None
                if wanted != last_vlans:
                    apply_vlans(driver, cfg, wanted, ports, reverse)
                    last_vlans = wanted
            if state.network:
                wanted, state.network = state.network, None
                apply_network(driver, cfg, wanted, state)
            status = "adopted" if state.data.get("authkey") else "waiting"
        except urllib.error.HTTPError as e:
            # 404 is normal before you click Adopt; 400 usually means the controller forgot our key.
            status, delay = f"HTTP {e.code}", 10
            if e.code == 400 and state.data.get("authkey") and cfg.get("reset_on_400", False):
                state.reset()
        except (DriverError, OSError, inform.InformError, ValueError) as e:
            status, delay = f"error: {e}", 15
        if status != last_status:
            log.info("%s", {"waiting": "waiting for adoption (click Adopt in UniFi)",
                            "HTTP 404": "controller sees us: click Adopt in UniFi Devices"}.get(status, status))
            last_status = status
        if once:
            return status
        time.sleep(delay)


def main():
    ap = argparse.ArgumentParser(prog="unifi-device-bridge", description=__doc__)
    ap.add_argument("-c", "--config", default="/etc/unifi-device-bridge/config.toml")
    ap.add_argument("--poll", action="store_true", help="read the switch once, print it and exit")
    ap.add_argument("--once", action="store_true", help="send one inform and exit")
    ap.add_argument("--reset", action="store_true", help="forget the adoption key (keeps the MAC)")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args()
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_path"] = args.config
    cfgs = device_configs(cfg)
    fmt = "%(message)s" if os.environ.get("INVOCATION_ID") else "%(asctime)s %(message)s"
    if len(cfgs) > 1:
        fmt = fmt.replace("%(message)s", "[%(threadName)s] %(message)s")
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stdout)
    if args.poll:
        for c in cfgs:
            if len(cfgs) > 1:
                print(f"== {c['name']}")
            snap = make_driver(c, int(c.get("ports") or report.model_info(c)[2])).poll()
            for p in snap.ports:
                print(f"{p.index:>2} {p.name:<12} {'up' if p.up else 'down':<4} {p.speed or '-':>5} "
                      f"rx={p.rx_bytes} tx={p.tx_bytes} {', '.join(m for m, _ in p.macs)}")
            for n in snap.neighbors:
                print(f"lldp port {n.port}: {n.name or n.chassis_id} {n.port_id}")
        return
    if args.reset:
        for c in cfgs:
            State(state_file(c)).reset()
        print("adoption reset")
        return
    if "inform_url" not in cfg:
        sys.exit("set inform_url in the config, e.g. http://192.168.1.1:8080/inform")
    print(run_all(cfgs, once=args.once))


if __name__ == "__main__":
    main()
