"""TP-Link Easy Smart switches (TL-SG105E, TL-SG108E, TL-SG108PE, TL-SG1016DE...).

They have no SNMP and no readable MAC table, so this scrapes the web UI for
port link state, speed and packet counters only. UniFi can't show which client
is on which port for these switches.

The switch counts packets, not bytes, so traffic in UniFi is an estimate:
packets times an average packet size (bytes_per_packet, 700 by default).

The web UI allows one session at a time, for the whole switch. The bridge logs in
once and keeps that session. If someone logs into the web page (which ends the
bridge's session), it backs off for webui_pause seconds (600) instead of
logging straight back in and kicking them out. UniFi keeps showing the last reading.
"""
import contextlib
import copy
import http.cookiejar
import ipaddress
import logging
import re
import threading
import time
import urllib.parse
import urllib.request

from ..model import DriverError, Port, Snapshot

# link_status codes from PortStatisticsRpm.htm -> (speed Mb/s, full duplex)
LINK = {2: (10, False), 3: (10, True), 4: (100, False), 5: (100, True), 6: (1000, True)}
# Average packet size for the byte estimate. Typical mixed home/office LAN traffic is 500-900 bytes
# (big downloads near 1500, calls and gaming near 200).
BYTES_PER_PACKET = 700
log = logging.getLogger("bridge")


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


def _ds(html, name):
    m = re.search(name + r"\s*=\s*\{(.*?)\}\s*;", html, re.S)
    if not m:
        raise DriverError(f"no {name} on the page")
    return m.group(1)


def _array(body, key):
    a = re.search(r"\b" + key + r"\s*:\s*\[([^\]]*)\]", body)
    return [int(x, 0) for x in re.findall(r"0x[0-9a-fA-F]+|\d+", a.group(1))] if a else []


def _ports(mask):
    return {p for p in range(1, 33) if mask >> (p - 1) & 1}


def parse_vlans(qvlan_html, pvid_html):
    """Vlan8021QRpm.htm + Vlan8021QPvidRpm.htm -> {"enabled", "ports", "vlans": {vid: {"name", "tagged", "untagged"}},
    "pvid": {port: vid}}. Port numbers are the switch's own."""
    q, pv = _ds(qvlan_html, "qvlan_ds"), _ds(pvid_html, "pvid_ds")
    n = re.search(r"portNum\s*:\s*(\d+)", q)
    names = re.search(r"names\s*:\s*\[([^\]]*)\]", q)
    names = re.findall(r'"([^"]*)"', names.group(1)) if names else []
    vids, tagged, untagged = _array(q, "vids"), _array(q, "tagMbrs"), _array(q, "untagMbrs")
    vlans = {vid: {"name": names[i] if i < len(names) else "",
                   "tagged": _ports(tagged[i]) if i < len(tagged) else set(),
                   "untagged": _ports(untagged[i]) if i < len(untagged) else set()} for i, vid in enumerate(vids)}
    return {"enabled": re.search(r"state\s*:\s*1\b", q) is not None, "ports": int(n.group(1)) if n else 0,
            "vlans": vlans, "pvid": dict(enumerate(_array(pv, "pvids"), 1))}


class EasySmartDriver:
    def __init__(self, cfg, port_count):
        self.base = "http://" + cfg["host"]
        self.username = cfg.get("username", "admin")
        self.password = cfg["password"]
        self.port_count = port_count
        self.bytes_per_packet = int(cfg.get("bytes_per_packet", BYTES_PER_PACKET))
        if not 64 <= self.bytes_per_packet <= 9000:
            raise DriverError("bytes_per_packet must be 64 to 9000")
        self.webui_pause = int(cfg.get("webui_pause", 600))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False
        self.paused_until = 0
        self.last = None
        self.lock = threading.RLock()   # the client scan uses the switch from its own thread

    def set_host(self, host):
        self.base = "http://" + host
        self.logged_in = False

    def _get(self, path, data=None):
        req = urllib.request.Request(self.base + path, data=data, headers={
            "Referer": self.base + "/Logout.htm", "User-Agent": "Mozilla/5.0"})
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.read().decode("utf-8", errors="replace")
        except OSError as e:
            self.logged_in = False
            raise DriverError(f"Easy Smart {self.base}: {e}") from None

    def page(self, path):
        """Fetch a page in the kept session, logging in when needed. The session is tied to
        the switch, not a cookie, so it lasts until someone else logs in or it times out."""
        with self.lock:
            return self._page(path)

    def _page(self, path):
        if time.time() < self.paused_until:
            raise DriverError("someone is using the switch's web UI: paused")
        fresh = not self.logged_in
        if fresh:
            form = {"username": self.username, "password": self.password, "cpassword": "", "logon": "Login"}
            self._get("/logon.cgi", urllib.parse.urlencode(form).encode())
        html = self._get(path)
        if "logonInfo" not in html:
            self.logged_in = True
            return html
        self.logged_in = False
        if fresh:
            raise DriverError("login failed (wrong username or password?)")
        # We were logged in and lost it: most likely someone opened the web UI. Leave them to it.
        self.paused_until = time.time() + self.webui_pause
        log.info("someone logged into the switch's web UI: pausing for %ss", self.webui_pause)
        raise DriverError("someone is using the switch's web UI: paused")

    def resume(self):
        """A change came from UniFi: end the web UI pause and apply it now (logs the web UI out)."""
        if time.time() < self.paused_until:
            log.info("change from UniFi: ending the web UI pause to apply it now")
            self.paused_until = 0

    @contextlib.contextmanager
    def session(self):
        """Yield a get(path) function using the kept session."""
        yield self.page

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
        with contextlib.suppress(DriverError):
            self.page("/ip_setting.cgi?" + urllib.parse.urlencode(params))
        self.logged_in = False

    def get_name(self):
        """The switch's Device Description (System Info page)."""
        m = re.search(r'descriStr\s*:\s*\[\s*"([^"]*)"', self.page("/SystemInfoRpm.htm"))
        return m.group(1) if m else None

    def set_name(self, name):
        """Device Description: letters, digits, hyphens and underscores, 32 max."""
        name = re.sub(r"[^0-9A-Za-z_-]", "", name)[:32]
        if name:
            self.page("/system_name_set.cgi?" + urllib.parse.urlencode({"sysName": name}))

    # Speed/duplex codes on the Port Setting page.
    SPEEDS = {"auto": 1, (10, False): 2, (10, True): 3, (100, False): 4, (100, True): 5, (1000, True): 6}

    def get_switching(self):
        """Speed codes per port, port mirroring and LAGs as the switch has them now."""
        ps, mi, tr = (self.page(p) for p in ("/PortSettingRpm.htm", "/PortMirrorRpm.htm", "/PortTrunkRpm.htm"))
        nums = lambda html, key: [int(x, 0) for x in re.findall(r"\w+", (re.search(key + r"\s*:\s*\[([^\]]*)\]", html) or [None, ""])[1])]
        speed = dict(enumerate(nums(ps, "spd_cfg")[:self.port_count], 1))
        en = re.search(r"MirrEn\s*=\s*(\d+)", mi)
        dest = int(re.search(r"MirrPort\s*=\s*(\d+)", mi).group(1)) if en and en.group(1) == "1" else None
        ing, egr = nums(mi, "ingress"), nums(mi, "egress")
        srcs = {i + 1 for i in range(self.port_count) if (ing[i:i + 1] or [0])[0] or (egr[i:i + 1] or [0])[0]}
        lags = {g: {i + 1 for i, v in enumerate(nums(tr, f"portStr_g{g}")) if v} for g in (1, 2)}
        return {"speed": speed, "mirror": (dest, srcs) if dest else None, "lags": {g: m for g, m in lags.items() if m}}

    def set_switching(self, want):
        """want: {"speed": {port: code}, "mirror": (dest, {sources}) or None, "lags": [{ports}]} (switch numbering)."""
        cur = self.get_switching()
        for p, code in sorted(want["speed"].items()):
            if cur["speed"].get(p) not in (None, code):
                log.info("switch port %s: speed/duplex code %s", p, code)
                self.page("/port_setting.cgi?" + urllib.parse.urlencode(
                    {"portid": p, "state": 7, "speed": code, "flowcontrol": 7, "apply": "Apply"}))
        if want["mirror"] != cur["mirror"]:
            if cur["mirror"]:
                self.page("/mirrored_port_set.cgi?" + urllib.parse.urlencode(
                    [("mirroredport", p) for p in sorted(cur["mirror"][1])] + [("ingressState", 0), ("egressState", 0), ("mirrored_submit", "Apply")]))
                self.page("/mirror_enabled_set.cgi?state=0&mirrorenable=Apply")
            if want["mirror"]:
                dest, srcs = want["mirror"]
                log.info("mirroring switch ports %s to port %s", sorted(srcs), dest)
                self.page(f"/mirror_enabled_set.cgi?state=1&mirroringport={dest}&mirrorenable=Apply")
                self.page("/mirrored_port_set.cgi?" + urllib.parse.urlencode(
                    [("mirroredport", p) for p in sorted(srcs)] + [("ingressState", 1), ("egressState", 1), ("mirrored_submit", "Apply")]))
            else:
                log.info("port mirroring off")
        # LAG groups: on 8-port models LAG 1 takes ports 1-4 and LAG 2 ports 5-8, 4 ports max each.
        wanted = {}
        for members in want["lags"]:
            half = self.port_count // 2
            g = 1 if max(members) <= half else 2 if min(members) > half else None
            if g is None or len(members) > 4 or g in wanted:
                log.warning("can't make a LAG of switch ports %s on this switch (LAG 1: ports 1-%s, LAG 2: %s-%s, 4 ports max)",
                            sorted(members), half, half + 1, self.port_count)
                continue
            wanted[g] = set(members)
        for g in (1, 2):
            if cur["lags"].get(g) == wanted.get(g):
                continue
            if g in cur["lags"]:
                self.page(f"/port_trunk_display.cgi?chk_trunk={g}&setDelete=Delete")
            if g in wanted:
                log.info("LAG %s: switch ports %s", g, sorted(wanted[g]))
                self.page("/port_trunk_set.cgi?" + urllib.parse.urlencode(
                    [("groupId", g)] + [("portid", p) for p in sorted(wanted[g])] + [("setapply", "Apply")]))

    def mirror_ports(self, dest, srcs, old=()):
        """Mirror the incoming traffic of srcs to dest (for the client scan). Each change is a flash write."""
        if not old:
            self.page(f"/mirror_enabled_set.cgi?state=1&mirroringport={dest}&mirrorenable=Apply")
        for ports, on in ((set(old) - set(srcs), 0), (srcs, 1)):
            if ports:
                self.page("/mirrored_port_set.cgi?" + urllib.parse.urlencode(
                    [("mirroredport", p) for p in sorted(ports)] + [("ingressState", on), ("egressState", 0), ("mirrored_submit", "Apply")]))

    def mirror_off(self):
        self.page("/mirrored_port_set.cgi?" + urllib.parse.urlencode(
            [("mirroredport", p) for p in range(1, self.port_count + 1)] + [("ingressState", 0), ("egressState", 0), ("mirrored_submit", "Apply")]))
        self.page("/mirror_enabled_set.cgi?state=0&mirrorenable=Apply")

    def reboot(self):
        """Restart the switch and wait until it answers again (about 40s)."""
        with self.lock:
            self.page("/SystemInfoRpm.htm")   # logged in
            with contextlib.suppress(DriverError):
                self._get("/reboot.cgi", urllib.parse.urlencode({"reboot_op": "reboot", "save_op": "false"}).encode())
            self.logged_in = False
            time.sleep(15)
            self.wait_back(timeout=120)

    def restore_defaults(self):
        """Back to stock for what the bridge changes: 802.1Q off (one plain network), every port on."""
        n = self.port_count
        self.set_vlans({"vlans": {1: {"tagged": set(), "untagged": set(range(1, n + 1))}}, "pvid": {p: 1 for p in range(1, n + 1)}})
        if self.get_vlans()["enabled"]:
            try:
                self.page("/qvlanSet.cgi?qvlan_en=0&qvlan_mode=Apply")
            except DriverError:
                pass   # the switch drops off for ~10s while it changes VLAN mode
            self.wait_back()
        for p in range(1, n + 1):
            self.set_port_enabled(p, True)
        self.set_switching({"speed": {p: 1 for p in range(1, n + 1)}, "mirror": None, "lags": []})

    def wait_back(self, timeout=60, step=3):
        """Wait until the switch answers again after a change that restarts its network."""
        end = time.time() + timeout
        while True:
            try:
                return self.get_vlans()
            except DriverError:
                if time.time() >= end:
                    raise
                time.sleep(step)

    def get_vlans(self):
        return parse_vlans(self.page("/Vlan8021QRpm.htm"), self.page("/Vlan8021QPvidRpm.htm"))

    def set_vlans(self, want):
        """Make 802.1Q VLANs and PVIDs match want ({"vlans", "pvid"}, switch port numbers)."""
        cur = self.get_vlans()
        n = cur["ports"] or self.port_count
        plain = {1: {"tagged": set(), "untagged": set(range(1, n + 1))}}
        if not cur["enabled"] and want["vlans"] == plain:
            return   # UniFi only has the default network: nothing to set up
        if not cur["enabled"]:
            log.info("enabling 802.1Q VLANs on the switch")
            self.page("/qvlanSet.cgi?qvlan_en=1&qvlan_mode=Apply")
            cur = self.get_vlans()
        for vid, v in sorted(want["vlans"].items()):
            c = cur["vlans"].get(vid)
            if c and c["tagged"] == v["tagged"] and c["untagged"] == v["untagged"]:
                continue
            sel = [(f"selType_{p}", 0 if p in v["untagged"] else 1 if p in v["tagged"] else 2) for p in range(1, n + 1)]
            self.page("/qvlanSet.cgi?" + urllib.parse.urlencode(
                [("vid", vid), ("vname", c["name"] if c else "")] + sel + [("qvlan_add", "Add/Modify")]))
        for vid in sorted(set(want["pvid"].values())):
            pbm = sum(1 << (p - 1) for p, pv in want["pvid"].items() if pv == vid and cur["pvid"].get(p) != vid)
            if pbm:
                self.page(f"/vlanPvidSet.cgi?pbm={pbm}&pvid={vid}")
        gone = sorted(vid for vid in cur["vlans"] if vid not in want["vlans"] and vid != 1)
        if gone:
            self.page("/qvlanSet.cgi?" + urllib.parse.urlencode([("selVlans", v) for v in gone] + [("qvlan_del", "Delete")]))
        # Read back and say if the switch didn't take it.
        got = self.get_vlans()
        bad = {p: got["pvid"].get(p) for p, vid in want["pvid"].items() if got["pvid"].get(p) != vid}
        if bad:
            log.warning("PVIDs didn't stick on switch ports %s (want %s)", bad, {p: want["pvid"][p] for p in bad})
        for vid, v in want["vlans"].items():
            g = got["vlans"].get(vid)
            if not g or g["tagged"] != v["tagged"] or g["untagged"] != v["untagged"]:
                log.warning("VLAN %s didn't stick: switch has %s", vid, g)
        log.info("switch PVIDs: %s", " ".join(f"{p}:{v}" for p, v in sorted(got["pvid"].items())))

    def poll(self):
        try:
            html = self.page("/PortStatisticsRpm.htm")
            info = self.page("/SystemInfoRpm.htm")
        except DriverError:
            # Web UI in use: keep showing the last reading rather than dropping off UniFi.
            if self.last and time.time() < self.paused_until:
                return copy.deepcopy(self.last)
            raise

        ports = []
        for n, (enabled, link, tx_good, tx_bad, rx_good, rx_bad) in enumerate(parse_stats(html)[:self.port_count], 1):
            speed, full = LINK.get(link, (None, None))
            ports.append(Port(
                index=n, name=f"Port {n}", up=link != 0, enabled=enabled,
                speed=speed, full_duplex=full,
                rx_packets=rx_good, tx_packets=tx_good, rx_errors=rx_bad, tx_errors=tx_bad,
                # Estimate: the switch only counts packets.
                rx_bytes=rx_good * self.bytes_per_packet, tx_bytes=tx_good * self.bytes_per_packet,
            ))
        m = re.search(r"macStr\s*:\s*\[\s*\"([0-9A-Fa-f:-]{17})\"", info)
        mac = m.group(1).replace("-", ":").lower() if m else None
        self.last = Snapshot(ports=ports, can_see_clients=False, description="TP-Link Easy Smart", mac=mac)
        return copy.deepcopy(self.last)   # a copy: the caller renumbers ports in place
