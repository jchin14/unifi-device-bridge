"""What a driver reads from a switch, independent of how it was read."""
from dataclasses import dataclass, field


@dataclass
class Port:
    index: int                   # UniFi port number, 1-based
    name: str = ""
    up: bool = False
    enabled: bool = True
    speed: int | None = None     # Mb/s, None if unknown
    full_duplex: bool | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_packets: int | None = None
    tx_packets: int | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None
    macs: list = field(default_factory=list)   # [(mac, vlan or None)]
    poe_enabled: bool | None = None   # None if the driver can't read PoE
    poe_good: bool | None = None      # powering a device
    poe_power: float | None = None    # watts


@dataclass
class Neighbor:
    port: int                    # UniFi port number
    chassis_id: str
    port_id: str = ""
    name: str = ""
    description: str = ""


@dataclass
class Snapshot:
    ports: list
    neighbors: list = field(default_factory=list)
    uptime: int | None = None    # switch uptime in seconds, if known
    description: str = ""
    can_see_clients: bool = True # False for drivers with no MAC table
    mac: str | None = None       # the switch's own MAC, if the driver can read it
    cpu: float | None = None     # percent, if known
    mem: float | None = None     # percent, if known
    mem_total: int | None = None # bytes, if known


class DriverError(RuntimeError):
    pass
