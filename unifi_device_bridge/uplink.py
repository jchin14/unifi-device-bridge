"""Find the uplink port by sending a burst of packets and seeing which switch ports count them.

Easy Smart switches have no MAC table or LLDP, so this is the only way to tell. It only reads
the switch (nothing is written to its flash):
1. Send packets to the controller (usually the gateway). If this machine is plugged into the switch, they come in on its
   port and go out the uplink, which shows as a jump in that port's transmit count.
2. Send packets to the switch itself. They come in on the port facing this machine. If step 1
   saw nothing, this machine is upstream, so that port is the uplink.
"""
import logging
import socket
import time
import urllib.parse

log = logging.getLogger("bridge")

BURST = 3000


def counts(driver):
    """{switch port: (rx_packets, tx_packets)} for ports that are up."""
    return {p.index: (p.rx_packets or 0, p.tx_packets or 0) for p in driver.poll().ports if p.up}


def burst(ip, n=BURST):
    """n small UDP packets to the discard port. Nothing needs to answer."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for i in range(n):
            try:
                s.sendto(b"unifi-device-bridge uplink check", (ip, 9))
            except OSError:
                time.sleep(0.01)   # send buffer full or ICMP error: slow down
            if i % 200 == 199:
                time.sleep(0.02)


def delta(before, after):
    return {p: (after[p][0] - before[p][0], after[p][1] - before[p][1]) for p in after if p in before}


def pick(to_gateway, to_switch, n=BURST):
    """-> (uplink, port facing this machine), each None if unclear. Ports in the switch's numbering."""
    need = n * 0.8
    mine = max((p for p, (rx, _) in to_switch.items() if rx >= need), key=lambda p: to_switch[p][0], default=None)
    out = [p for p, (_, tx) in to_gateway.items() if tx >= need and p != mine]
    if out:
        return max(out, key=lambda p: to_gateway[p][1]), mine
    # Nothing crossed the switch to the gateway: this machine is upstream, reached via the uplink.
    return mine, mine


def find(driver, gateway, switch_ip, settle=2):
    a = counts(driver)
    burst(gateway)
    time.sleep(settle)
    b = counts(driver)
    burst(switch_ip)
    time.sleep(settle)
    c = counts(driver)
    return pick(delta(a, b), delta(b, c))


def check(driver, cfg):
    """Log where the uplink seems to be, and warn if uplink_port says otherwise. Never changes the config."""
    switch_ip = getattr(driver, "base", "").removeprefix("http://")
    if not switch_ip:
        return None
    controller = urllib.parse.urlsplit(cfg["inform_url"]).hostname
    try:
        up, mine = find(driver, socket.gethostbyname(controller), switch_ip)
    except (OSError, RuntimeError) as e:
        log.info("uplink check skipped: %s", e)
        return None
    if up is None:
        log.info("uplink check: couldn't tell which port is the uplink (traffic too busy?)")
        return None
    where = f", this machine is on port {mine}" if mine and mine != up else ""
    set_to = cfg.get("_uplink_port_cfg")
    if set_to and set_to != up and cfg.get("follow_uplink", True):
        log.info("uplink check: uplink is port %s%s (uplink_port = %s in the config): using port %s", up, where, set_to, up)
    elif set_to and set_to != up:
        log.warning("uplink check: uplink looks like port %s%s, but uplink_port = %s. Fix it in the config", up, where, set_to)
    else:
        log.info("uplink check: uplink is port %s%s", up, where)
    return up, mine
