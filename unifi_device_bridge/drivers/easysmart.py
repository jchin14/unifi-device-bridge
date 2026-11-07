"""TP-Link Easy Smart switches (TL-SG105E, TL-SG108E, TL-SG108PE, TL-SG1016DE...).

They have no SNMP and no readable MAC table, so this scrapes the web UI for
port link state, speed and packet counters only. UniFi can't show which client
is on which port for these switches.

The web UI allows one session at a time: polling will log you out of the
switch's web page if you have it open, and vice versa.
"""
import contextlib
import http.cookiejar
import ipaddress
import re
import urllib.parse
import urllib.request

from ..model import DriverError, Port, Snapshot

# link_status codes from PortStatisticsRpm.htm -> (speed Mb/s, full duplex)
LINK = {2: (10, False), 3: (10, True), 4: (100, False), 5: (100, True), 6: (1000, True)}


def parse_stats(html):
    """PortStatisticsRpm.htm -> list of (enabled, link_status, tx_good, tx_bad, rx_good, rx_bad)."""
    m = re.search(r"all_info\s*=\s*\{(.*?)\}\s*;", html, re.S)
    if not m:
        raise DriverError("no port statistics on the page (wrong password, or someone is logged into the web UI?)")
    info = m.group(1)

    def array(name):
        a = re.search(name + r"\s*:\s*\[([^\]]*)\]", info)
        return [int(x) for x in re.findall(r"-?\d+", a.group(1))] if a else []

    count = re.search(r"max_port_num\s*=\s*(\d+)", html)
    state, link, pkts = array("state"), array("link_status"), array("pkts")
    count = int(count.group(1)) if count else len(link)
    rows = []
    for i in range(count):
        p = pkts[i * 4:i * 4 + 4] + [0, 0, 0, 0]
        rows.append((state[i] == 1 if i < len(state) else True, link[i] if i < len(link) else 0, *p[:4]))
    return rows


def parse_ip(html):
    """IpSettingRpm.htm -> {"dhcp", "ip", "netmask", "gateway"}."""
    m = re.search(r"ip_ds\s*=\s*\{(.*?)\}", html, re.S)
    if not m:
        raise DriverError("no IP settings on the page")

    def field(name):
        f = re.search(name + r"\s*:\s*\[?\s*\"?([0-9.]+)", m.group(1))
        return f.group(1) if f else None

    net = {"dhcp": field("state") == "1", "ip": field("ipStr"), "netmask": field("netmaskStr"),
           "gateway": field("gatewayStr")}
    ipaddress.IPv4Address(net["ip"] or "")
    return net


class EasySmartDriver:
    def __init__(self, cfg, port_count):
        self.base = "http://" + cfg["host"]
        self.username = cfg.get("username", "admin")
        self.password = cfg["password"]
        self.port_count = port_count

    def set_host(self, host):
        self.base = "http://" + host

    @contextlib.contextmanager
    def session(self):
        """Log in, yield a get(path) function, log out."""
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        headers = {"Referer": self.base + "/Logout.htm", "User-Agent": "Mozilla/5.0"}

        def get(path, data=None):
            req = urllib.request.Request(self.base + path, data=data, headers=headers)
            with opener.open(req, timeout=10) as r:
                return r.read().decode("utf-8", errors="replace")

        try:
            form = {"username": self.username, "password": self.password, "cpassword": "", "logon": "Login"}
            get("/logon.cgi", urllib.parse.urlencode(form).encode())
            yield get
        except OSError as e:
            raise DriverError(f"Easy Smart {self.base}: {e}") from None
        finally:
            with contextlib.suppress(OSError):
                get("/Logout.htm")

    def set_port_enabled(self, port, enabled):
        """Port Setting page: state 0 = disabled, 1 = enabled; 7 leaves speed/flow control as they are."""
        params = {"portid": port, "state": 1 if enabled else 0, "speed": 7, "flowcontrol": 7, "apply": "Apply"}
        with self.session() as get:
            get("/port_setting.cgi?" + urllib.parse.urlencode(params))

    def get_ip(self):
        """Current IP settings: {"dhcp", "ip", "netmask", "gateway"}."""
        with self.session() as get:
            page = get("/IpSettingRpm.htm")
        return parse_ip(page)

    def set_ip(self, net):
        """Apply UniFi's IP settings. The switch may move to the new IP before it answers."""
        if net["dhcp"]:
            params = {"dhcpSetting": "enable"}
        else:
            params = {"dhcpSetting": "disable", "ip_address": net["ip"], "ip_netmask": net["netmask"],
                      "ip_gateway": net.get("gateway") or ""}
        with self.session() as get:
            with contextlib.suppress(OSError):
                get("/ip_setting.cgi?" + urllib.parse.urlencode(params))

    def poll(self):
        with self.session() as get:
            html = get("/PortStatisticsRpm.htm")
            info = get("/SystemInfoRpm.htm")

        ports = []
        for n, (enabled, link, tx_good, tx_bad, rx_good, rx_bad) in enumerate(parse_stats(html)[:self.port_count], 1):
            speed, full = LINK.get(link, (None, None))
            ports.append(Port(
                index=n, name=f"Port {n}", up=link != 0, enabled=enabled,
                speed=speed, full_duplex=full,
                rx_packets=rx_good, tx_packets=tx_good, rx_errors=rx_bad, tx_errors=tx_bad,
            ))
        m = re.search(r"macStr\s*:\s*\[\s*\"([0-9A-Fa-f:-]{17})\"", info)
        mac = m.group(1).replace("-", ":").lower() if m else None
        return Snapshot(ports=ports, can_see_clients=False, description="TP-Link Easy Smart", mac=mac)
