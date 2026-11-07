"""Made-up switch for testing adoption without hardware. Reports no clients."""
import time

from ..model import Port, Snapshot

START = time.time()


class FakeDriver:
    def __init__(self, cfg, port_count):
        self.port_count = port_count
        self.up = set(cfg.get("up_ports", [1, 2]))

    def poll(self):
        t = int(time.time() - START)
        ports = []
        for n in range(1, self.port_count + 1):
            up = n in self.up
            ports.append(Port(
                index=n, name=f"Port {n}", up=up, speed=1000 if up else None, full_duplex=True if up else None,
                rx_bytes=t * 125_000 * up, tx_bytes=t * 50_000 * up,
                rx_packets=t * 100 * up, tx_packets=t * 60 * up, rx_errors=0, tx_errors=0,
            ))
        return Snapshot(ports=ports, uptime=t, description="fake switch")
