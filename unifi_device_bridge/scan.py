"""Find which clients are on which port of a switch with no MAC table (Easy Smart), using port mirroring.

Each round mirrors the incoming traffic of some ports to the port this machine is on, and notes the
source MACs of the copies. Ports get codes 1..n and round b mirrors the ports whose code has bit b
set, so a MAC's port is the sum of the rounds it showed up in: 7 ports take 3 rounds.

Every mirror change is written to the switch's flash, so scans are rare (about 8 writes each).
"""
import ipaddress
import logging
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.parse

from . import uplink
from .model import DriverError, Neighbor

log = logging.getLogger("bridge")

ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_PROMISC = 1
PACKET_OUTGOING = 4
ETH_LLDP = b"\x88\xcc"
LLDP_GROUP = bytes.fromhex("0180c200000e")


def tlv(t, value):
    return struct.pack("!H", t << 9 | len(value)) + value


def lldp_frame(dst, src, chassis, port, name):
    """LLDP saying "I'm chassis/name, you're on my port"."""
    label = f"Port {port}".encode()
    return (dst + src + ETH_LLDP + tlv(1, b"\x04" + chassis) + tlv(2, b"\x07" + label)
            + tlv(3, struct.pack("!H", 120)) + tlv(4, label) + tlv(5, name.encode()) + tlv(0, b""))


def lldp(frame):
    """LLDP frame -> {"chassis", "port", "name", "descr"}, or None. The switch passes these through."""
    if frame[12:14] != ETH_LLDP:
        return None
    out, i = {}, 14
    while i + 2 <= len(frame):
        t, n = frame[i] >> 1, (frame[i] & 1) << 8 | frame[i + 1]
        v, i = frame[i + 2:i + 2 + n], i + 2 + n
        if t == 0:
            break
        if t in (1, 2) and v:
            sub, val = v[0], v[1:]
            text = val.hex(":") if (t, sub) in ((1, 4), (2, 3)) else val.decode(errors="replace")
            out["chassis" if t == 1 else "port"] = text
        elif t in (4, 5, 6):
            out[{4: "port_descr", 5: "name", 6: "descr"}[t]] = v.decode(errors="replace")
    return out if "chassis" in out else None


def rounds(ports):
    """Which ports to mirror in each round."""
    codes = {p: i for i, p in enumerate(sorted(ports), 1)}
    return [{p for p, i in codes.items() if i >> b & 1} for b in range(len(codes).bit_length())]


def locate(seen, ports):
    """seen: the MACs found in each round -> {port: [macs]}. A MAC with an impossible code is dropped."""
    by_code = {i: p for i, p in enumerate(sorted(ports), 1)}
    out = {}
    for mac in set().union(*seen) if seen else ():
        p = by_code.get(sum(1 << b for b, s in enumerate(seen) if mac in s))
        if p:
            out.setdefault(p, []).append(mac)
    return {p: sorted(m) for p, m in out.items()}


class Evidence:
    """Source MACs of frames that must be mirror copies: unicast not addressed to this machine, or the
    same frame twice within 50 ms (the normal copy plus the mirrored one)."""

    def __init__(self, mine, flooded=(), flooders=(), cap=100000):
        self.mine, self.flooded, self.flooders = mine, set(flooded), set(flooders)
        self.cap, self.macs, self.recent = cap, set(), {}
        self.dsts = set()     # unicast destinations not ours (the baseline uses these to find flooded ones)
        self.senders = set()  # who sent them: upstream hosts whose flooded unicast reaches us anyway
        self.lldp = {}        # source MAC -> LLDP info, from any port (the switch floods LLDP)

    def add(self, frame, now=None):
        if len(frame) < 14:
            return
        dst, src = frame[:6], frame[6:12]
        if src[0] & 1 or src == self.mine:
            return
        mac = src.hex(":")
        if mac not in self.lldp and (info := lldp(frame)):
            self.lldp[mac] = info
        if mac in self.macs:
            return
        if not dst[0] & 1 and dst != self.mine:
            self.dsts.add(dst)
            self.senders.add(mac)
            if dst not in self.flooded and mac not in self.flooders:
                self.macs.add(mac)
            return
        now = time.monotonic() if now is None else now
        key = hash(frame)
        if now - self.recent.get(key, -1) < 0.05:
            self.macs.add(mac)
        elif len(self.recent) < self.cap:
            self.recent[key] = now


def link(switch_ip):
    """(interface, its IP, its MAC as bytes) used to reach the switch."""
    out = subprocess.run(["ip", "-o", "route", "get", switch_ip], capture_output=True, text=True, check=True).stdout
    iface = re.search(r"\bdev (\S+)", out).group(1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((switch_ip, 9))
        ip = s.getsockname()[0]
    with open(f"/sys/class/net/{iface}/address") as f:
        return iface, ip, bytes.fromhex(f.read().strip().replace(":", ""))


def gateway_mac(ip):
    """MAC of a host on this LAN from the ARP table (pinged first so it's there)."""
    subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True)
    with open("/proc/net/arp") as f:
        for line in list(f)[1:]:
            a, _hw, flags, mac = line.split()[:4]
            if a == ip and int(flags, 16) & 2:
                return bytes.fromhex(mac.replace(":", ""))
    return None


def targets(iface, my_ip, limit=4094):
    """Every address in this machine's subnet on iface, if it's small enough."""
    out = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", iface], capture_output=True, text=True).stdout
    for cidr in re.findall(r"inet (\S+)", out):
        net = ipaddress.ip_interface(cidr).network
        if cidr.startswith(my_ip + "/") and net.num_addresses - 2 <= limit:
            return [str(h) for h in net.hosts() if str(h) != my_ip]
    return []


def arp_probe(mine, reply_to, ip):
    """Broadcast ARP probe (sender IP 0.0.0.0, so no one's ARP cache changes). Hosts answer to
    reply_to (the gateway's MAC), so the answer leaves through the uplink and only reaches this
    machine as a mirror copy."""
    return b"\xff" * 6 + mine + b"\x08\x06" + struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, 1, reply_to,
                                                         b"\0" * 4, b"\0" * 6, socket.inet_aton(ip))


def sniff(iface, seconds, mine, probes=(), flooded=(), flooders=()):
    """-> Evidence of the frames seen. Sends the probes first."""
    ev = Evidence(mine, flooded, flooders)
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)) as s:
        s.bind((iface, 0))
        # Mirror copies are addressed to other machines: receive them too while the socket is open.
        s.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP,
                     struct.pack("iHH8s", socket.if_nametoindex(iface), PACKET_MR_PROMISC, 0, b""))
        for i, frame in enumerate(probes):
            try:
                s.send(frame)
            except OSError:
                time.sleep(0.01)
            if i % 100 == 99:
                time.sleep(0.01)
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            s.settimeout(left)
            try:
                frame, addr = s.recvfrom(65535)
            except TimeoutError:
                break
            if addr[2] != PACKET_OUTGOING:
                ev.add(frame)
    return ev


class Scanner:
    """Runs scans in the background: at start, every scan_interval seconds and when asked."""

    def __init__(self, driver, cfg, mirror_in_use=lambda: False, on_uplink=lambda port: None, me=None):
        self.driver, self.cfg, self.mirror_in_use, self.on_uplink = driver, cfg, mirror_in_use, on_uplink
        self.me = me   # -> (device MAC, name, switch port -> UniFi port) for the LLDP we send
        self.interval = int(cfg.get("scan_interval", 43200))
        self.seconds = int(cfg.get("scan_seconds", 10))
        self.results, self.done, self.dirty, self.mine = {}, False, False, None
        self.lldp = {}
        self.lock = threading.Lock()      # held while the switch's mirror is in use
        self.wanted = threading.Event()
        self.reason = "start"
        self.wanted.set()
        threading.Thread(target=self.loop, daemon=True, name=threading.current_thread().name).start()
        threading.Thread(target=self.listen, daemon=True, name=threading.current_thread().name).start()

    def listen(self):
        """Keep the latest LLDP from each sender (APs send every 30s or so, a scan may miss it)."""
        try:
            iface = link(self.driver.base.removeprefix("http://"))[0]
            with open(f"/sys/class/net/{iface}/address") as f:
                src = bytes.fromhex(f.read().strip().replace(":", ""))
            with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x88CC)) as s:
                s.bind((iface, 0))
                s.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, struct.pack(
                    "iHH8s", socket.if_nametoindex(iface), 0, 6, bytes.fromhex("0180c200000e") + b"\0\0"))
                s.settimeout(30)
                next_send = 0
                while True:
                    if self.me and self.cfg.get("lldp_announce", True) and time.monotonic() >= next_send:
                        self.announce(s, src)
                        next_send = time.monotonic() + 30
                    try:
                        frame = s.recv(2000)
                    except TimeoutError:
                        continue
                    if (info := lldp(frame)) and frame[6:12] != src:
                        self.lldp[frame[6:12].hex(":")] = info
        except Exception as e:
            log.warning("LLDP listener stopped: %s", e)

    def busy(self):
        return self.lock.locked() or self.dirty

    def fill(self, snap):
        """Put the last scan's clients on the snapshot's ports (switch numbering, before reordering)."""
        if not self.done:
            return snap
        found = {p: list(m) for p, m in self.results.items()}
        if self.mine:
            found.setdefault(self.mine[0], []).append(self.mine[1])
        for p in snap.ports:
            p.macs = [(m, None) for m in found.get(p.index, [])]
        snap.neighbors = list(snap.neighbors) + self.neighbors()
        snap.can_see_clients = True
        return snap

    def announce(self, s, src):
        """Tell the UniFi device found on a port which port it's on. UniFi devices ignore LLDP sent to
        their own MAC, and the switch floods LLDP to every port, so this only works with one of them."""
        mac, name, to_unifi = self.me()
        found = self.neighbors()
        now = sorted((n.chassis_id, n.port) for n in found)
        if now != getattr(self, "_told", None):
            self._told = now
            if len(found) == 1:
                log.info("LLDP: telling %s it's on port %s", found[0].chassis_id, to_unifi(found[0].port))
            elif found:
                log.info("LLDP: %s LLDP devices on the switch, not sending (they'd all get one port)", len(found))
        if len(found) != 1:
            return
        try:
            s.send(lldp_frame(LLDP_GROUP, src, bytes.fromhex(mac.replace(":", "")), to_unifi(found[0].port), name))
        except OSError as e:
            log.warning("LLDP send failed: %s", e)

    def neighbors(self):
        """LLDP senders (e.g. UniFi APs) whose MAC the scan put on a port, so UniFi links them here."""
        where = {m: p for p, macs in self.results.items() for m in macs}
        out = []
        for src, info in sorted(self.lldp.items()):
            port = where.get(src) or where.get(info["chassis"])
            if port:
                out.append(Neighbor(port=port, chassis_id=info["chassis"],
                                    port_id=info.get("port_descr") or info.get("port", ""),
                                    name=info.get("name", ""), description=info.get("descr", "")))
        return out

    def request(self, reason):
        if not self.wanted.is_set():
            self.reason = reason
            self.wanted.set()

    def loop(self):
        next_at = time.time() + self.interval
        while True:
            asked = self.wanted.wait(timeout=60)
            if self.dirty:
                with self.lock:
                    self.clean()
            if not asked and (self.interval <= 0 or time.time() < next_at):
                continue
            reason = self.reason if asked else "scheduled"
            self.wanted.clear()
            try:
                self.scan(reason)
            except Exception as e:   # keep the scan thread alive whatever goes wrong
                log.warning("client scan failed: %s", e)
            next_at = time.time() + self.interval

    def scan(self, reason):
        if self.mirror_in_use():
            log.info("client scan skipped: a port mirror is set in UniFi (the switch has only one)")
            return
        d = self.driver
        switch_ip = d.base.removeprefix("http://")
        gateway = socket.gethostbyname(urllib.parse.urlsplit(self.cfg["inform_url"]).hostname)
        with self.lock:
            if d.get_switching()["mirror"]:
                log.info("client scan skipped: port mirroring is already on in the switch")
                return
            up, mine = uplink.find(d, gateway, switch_ip)
            if up:
                self.on_uplink(up)   # the uplink cable may have moved since the last scan
            if not mine or mine == up:
                log.info("client scan skipped: this machine isn't plugged into the switch (or its port is unclear)")
                return
            iface, my_ip, my_mac = link(switch_ip)
            ports = [p.index for p in d.poll().ports if p.up and p.index not in (up, mine)]
            plan = rounds(ports)
            gw = gateway_mac(gateway)
            probes = [arp_probe(my_mac, gw, ip) for ip in targets(iface, my_ip)] if gw else []
            log.info("client scan (%s): switch ports %s, mirrored to port %s in %s rounds", reason, ports, mine, len(plan))
            # Before mirroring: unicast that reaches us anyway (flooded) isn't proof of a port.
            base = sniff(iface, self.seconds, my_mac)
            flooded, flooders = base.dsts, base.senders | ({gw.hex(":")} if gw else set())
            seen, prev, heard = [], set(), dict(base.lldp)
            try:
                for srcs in plan:
                    d.mirror_ports(mine, srcs, prev)
                    prev = srcs
                    time.sleep(1)
                    ev = sniff(iface, self.seconds, my_mac, probes, flooded, flooders)
                    heard.update(ev.lldp)
                    seen.append(ev.macs - {gw.hex(":") if gw else ""})
            finally:
                if prev:
                    self.clean()
        self.mine = (mine, my_mac.hex(":"))
        self.lldp.update(heard)
        self.results, self.done = locate(seen, ports), True
        log.info("client scan: %s", ", ".join(f"port {p}: {len(m)}" for p, m in sorted(self.results.items())) or "no clients seen")

    def clean(self):
        """Turn mirroring off, retrying. If the switch won't answer, keep trying every minute."""
        for _ in range(3):
            try:
                self.driver.mirror_off()
                self.dirty = False
                return
            except DriverError as e:
                err = e
                time.sleep(5)
        if not self.dirty:
            log.warning("client scan: couldn't turn port mirroring off (%s), will keep trying", err)
        self.dirty = True
