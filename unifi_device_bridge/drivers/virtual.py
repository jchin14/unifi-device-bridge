"""Virtual device: no hardware, just believable numbers so it looks real in UniFi.

Traffic moves with a slow daily-ish wave plus some noise, PoE ports draw power, and port or
PoE changes made in UniFi are remembered (in memory) so the toggles appear to work.
"""
import math
import random
import time
import zlib

from ..model import Port, Snapshot


class VirtualDriver:
    def __init__(self, cfg, port_count, seed=""):
        self.port_count = port_count
        rnd = random.Random(zlib.crc32(seed.encode()))
        if "up_ports" in cfg:
            self.up = set(cfg["up_ports"])
        else:   # a believable mix: uplink plus roughly 60% of the rest
            self.up = {1} | {n for n in range(2, port_count + 1) if rnd.random() < 0.6}
        self.poe_watts = float(cfg.get("poe_watts", 4.5))
        # Per port: base rate (bytes/s), phase, and a fixed speed so it doesn't flap.
        self.profile = {n: (rnd.uniform(20_000, 2_000_000) * (8 if n == 1 else 1), rnd.uniform(0, 2 * math.pi),
                            100 if rnd.random() < 0.15 and n != 1 else 1000, rnd.uniform(0.5, 1.5))
                        for n in range(1, port_count + 1)}
        self.disabled = set()
        self.poe_off = set()
        # Pretend it's been up for a few days, with traffic to match.
        self.boot = time.time() - rnd.uniform(2, 40) * 86400
        self.rx = {n: (self.profile[n][0] * 0.6 * (time.time() - self.boot) if n in self.up else 0.0)
                   for n in self.profile}
        self.tx = {n: v * 0.35 for n, v in self.rx.items()}
        self.last = time.time()
        self.rnd = random.Random()

    def _advance(self):
        now = time.time()
        dt = max(0.0, now - self.last)
        self.last = now
        for n, (rate, phase, _speed, _poe) in self.profile.items():
            if not self._up(n):
                continue
            wave = 0.6 + 0.4 * math.sin(now / 3600 * 2 * math.pi / 6 + phase)   # 6-hour cycle
            r = rate * wave * self.rnd.uniform(0.7, 1.3)
            self.rx[n] += r * dt
            self.tx[n] += r * dt * 0.35

    def _up(self, n):
        return n in self.up and n not in self.disabled

    def poll(self):
        self._advance()
        ports = []
        for n in range(1, self.port_count + 1):
            up = self._up(n)
            _rate, _phase, speed, poe_scale = self.profile[n]
            rx, tx = int(self.rx[n]), int(self.tx[n])
            ports.append(Port(
                index=n, name=f"Port {n}", up=up, enabled=n not in self.disabled,
                speed=speed if up else None, full_duplex=True if up else None,
                rx_bytes=rx, tx_bytes=tx, rx_packets=rx // 900, tx_packets=tx // 700, rx_errors=0, tx_errors=0,
                poe_enabled=n not in self.poe_off,
                poe_good=up and n not in self.poe_off and n != 1,
                poe_power=round(self.poe_watts * poe_scale, 2) if up and n not in self.poe_off and n != 1 else 0.0,
            ))
        return Snapshot(ports=ports, uptime=int(time.time() - self.boot), description="virtual device",
                        can_see_clients=False)

    # UniFi port controls: remembered so the UI looks like it worked.
    def set_port_enabled(self, port, enabled):
        (self.disabled.discard if enabled else self.disabled.add)(port)

    def set_poe(self, port, on):
        (self.poe_off.discard if on else self.poe_off.add)(port)
