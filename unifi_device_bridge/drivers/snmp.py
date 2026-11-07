"""Any managed switch with SNMP: IF-MIB ports, BRIDGE/Q-BRIDGE MAC table, LLDP.

Uses the net-snmp command line tools (apt install snmp), so there are no Python deps.
"""
import subprocess

from ..model import DriverError, Neighbor, Port, Snapshot

SYSTEM = "1.3.6.1.2.1.1"
IF_TABLE = "1.3.6.1.2.1.2.2.1"           # 2 descr, 3 type, 6 phys addr, 7 admin, 8 oper, 14/20 errors
IFX_TABLE = "1.3.6.1.2.1.31.1.1.1"       # 1 name, 6/10 HC octets, 7/11 HC ucast pkts, 15 high speed
DUPLEX = "1.3.6.1.2.1.10.7.2.1.19"       # dot3StatsDuplexStatus: 2 half, 3 full
BRIDGE_ADDRESS = "1.3.6.1.2.1.17.1.1"         # dot1dBaseBridgeAddress
BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
QBRIDGE_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"   # .fdbId.mac(6) = bridge port
QBRIDGE_FDB_STATUS = "1.3.6.1.2.1.17.7.1.2.2.1.3" # 3 learned, 4 self, 5 mgmt
BRIDGE_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"        # .mac(6) = bridge port
BRIDGE_FDB_STATUS = "1.3.6.1.2.1.17.4.3.1.3"
PSE_ADMIN = "1.3.6.1.2.1.105.1.1.1.3"   # pethPsePortAdminEnable.group.port: 1 on, 2 off
PSE_STATUS = "1.3.6.1.2.1.105.1.1.1.6"  # pethPsePortDetectionStatus: 3 delivering power
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"      # .col.timeMark.localPort.index
ETHERNET_TYPES = {6, 117}                # ethernetCsmacd, gigabitEthernet


def _value(typ, raw):
    """Turn one net-snmp value into int, bytes or str."""
    if typ in ("INTEGER", "Counter32", "Counter64", "Gauge32", "Unsigned32", "Timeticks"):
        raw = raw.split(")")[0].lstrip("(") if raw.startswith("(") else raw.split()[0]
        try:
            return int(raw)
        except ValueError:
            return None
    if typ == "Hex-STRING":
        return bytes.fromhex("".join(raw.split()))
    if typ == "STRING":
        return raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
    return raw


def parse_walk(out, base):
    """net-snmp -On output -> {oid suffix: value}."""
    rows = []
    for line in out.splitlines():
        if line.startswith(".") and " = " in line:
            oid, _, val = line.partition(" = ")
            rows.append([oid[1:], val])
        elif rows:
            rows[-1][1] += "\n" + line   # strings and long hex values can wrap
    result = {}
    prefix = base + "."
    for oid, val in rows:
        if not oid.startswith(prefix):
            continue
        typ, sep, raw = val.partition(": ")
        if not sep:
            if val.startswith('""'):
                typ, raw = "STRING", ""
            else:
                continue                # "No Such Object", "No more variables", ...
        result[oid[len(prefix):]] = _value(typ, raw.strip())
    return result


def column(table, col):
    """{'8.3': v} rows of one table column -> {'3': v}."""
    head = f"{col}."
    return {k[len(head):]: v for k, v in table.items() if k.startswith(head)}


def mac_str(raw):
    if isinstance(raw, bytes) and len(raw) == 6:
        return ":".join(f"{b:02x}" for b in raw)
    return None


def text(raw):
    if isinstance(raw, bytes):
        mac = mac_str(raw)
        return mac or raw.decode(errors="replace").strip("\0")
    return "" if raw is None else str(raw)


class SnmpDriver:
    def __init__(self, cfg, port_count):
        self.host = cfg["host"]
        self.port_count = port_count
        self.ports = cfg.get("ports")            # ifIndex of each UniFi port, in order
        self.if_indexes = []
        version = str(cfg.get("version", "2c"))
        if version == "3":
            self.auth = ["-v3", "-u", cfg["user"]]
            if cfg.get("priv_password"):
                self.auth += ["-l", "authPriv", "-a", cfg.get("auth_protocol", "SHA"), "-A", cfg["auth_password"],
                              "-x", cfg.get("priv_protocol", "AES"), "-X", cfg["priv_password"]]
            elif cfg.get("auth_password"):
                self.auth += ["-l", "authNoPriv", "-a", cfg.get("auth_protocol", "SHA"), "-A", cfg["auth_password"]]
            else:
                self.auth += ["-l", "noAuthNoPriv"]
        elif version in ("1", "2c"):
            self.auth = ["-v" + version, "-c", cfg.get("community", "public")]
        else:
            raise DriverError(f"unknown SNMP version {version!r}")
        self.bulk = version != "1"
        self.write_community = cfg.get("write_community")   # v1/v2c only; needed for port control

    def set_host(self, host):
        self.host = host

    def snmpset(self, oid, value):
        if self.auth[0] == "-v3":
            auth = self.auth
        elif self.write_community:
            auth = [self.auth[0], "-c", self.write_community]
        else:
            raise DriverError("set write_community in [snmp] to control ports")
        out = subprocess.run(["snmpset", *auth, "-t", "3", "-r", "1", self.host, oid, "i", str(value)],
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise DriverError(f"SNMP set {oid}: {out.stderr.strip() or 'failed'}")

    def set_port_enabled(self, port, enabled):
        self.snmpset(f"{IF_TABLE}.7.{self.if_index(port)}", 1 if enabled else 2)

    def set_poe(self, port, on):
        self.snmpset(f"{PSE_ADMIN}.1.{self.pse_index(port)}", 1 if on else 2)

    def if_index(self, port):
        return self.if_indexes[port - 1]

    def pse_index(self, port):
        # PSE ports are usually numbered like the front panel (group 1).
        return port

    def walk(self, base, required=False):
        cmd = ["snmpbulkwalk" if self.bulk else "snmpwalk", *self.auth, "-On", "-Oe", "-Ot", "-t", "3", "-r", "1"]
        if self.bulk:
            cmd += ["-Cr25"]
        cmd += [self.host, base]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise DriverError("net-snmp tools not found: apt install snmp") from None
        except subprocess.TimeoutExpired:
            raise DriverError(f"SNMP walk of {base} timed out") from None
        if out.returncode and (required or "Timeout" in out.stderr):
            raise DriverError(f"SNMP {self.host}: {out.stderr.strip() or 'failed'}")
        return parse_walk(out.stdout, base)

    def poll(self):
        system = self.walk(SYSTEM, required=True)
        if_table = self.walk(IF_TABLE, required=True)
        ifx = self.walk(IFX_TABLE)

        types = column(if_table, 3)
        if self.ports:
            if_indexes = [str(i) for i in self.ports]
        else:
            if_indexes = sorted((i for i, t in types.items() if t in ETHERNET_TYPES), key=int)
        if_indexes = if_indexes[:self.port_count]
        self.if_indexes = if_indexes
        unifi_port = {ifi: n for n, ifi in enumerate(if_indexes, 1)}

        names, descr = column(ifx, 1), column(if_table, 2)
        admin, oper = column(if_table, 7), column(if_table, 8)
        high_speed = column(ifx, 15)
        rx_bytes, tx_bytes = column(ifx, 6), column(ifx, 10)
        rx_pkts, tx_pkts = column(ifx, 7), column(ifx, 11)
        rx_err, tx_err = column(if_table, 14), column(if_table, 20)
        duplex = self.walk(DUPLEX)
        own_macs = {mac_str(v) for v in column(if_table, 6).values()}

        ports = []
        for ifi in if_indexes:
            up = oper.get(ifi) == 1
            ports.append(Port(
                index=unifi_port[ifi],
                name=text(names.get(ifi) or descr.get(ifi)),
                up=up,
                enabled=admin.get(ifi, 1) == 1,
                speed=high_speed.get(ifi) if up else None,
                full_duplex={3: True, 2: False}.get(duplex.get(ifi)),
                rx_bytes=rx_bytes.get(ifi), tx_bytes=tx_bytes.get(ifi),
                rx_packets=rx_pkts.get(ifi), tx_packets=tx_pkts.get(ifi),
                rx_errors=rx_err.get(ifi), tx_errors=tx_err.get(ifi),
            ))
        by_index = {p.index: p for p in ports}

        pse_admin, pse_status = self.walk(PSE_ADMIN), self.walk(PSE_STATUS)
        for key, val in pse_admin.items():
            n = int(key.split(".")[-1])
            if n in by_index:
                by_index[n].poe_enabled = val == 1
                by_index[n].poe_good = pse_status.get(key) == 3

        base_to_if = {k: str(v) for k, v in self.walk(BASE_PORT_IFINDEX).items()}

        def port_for(bridge_port):
            return unifi_port.get(base_to_if.get(str(bridge_port), str(bridge_port)))

        fdb, status, vlan_aware = self.walk(QBRIDGE_FDB_PORT), self.walk(QBRIDGE_FDB_STATUS), True
        if not fdb:
            fdb, status, vlan_aware = self.walk(BRIDGE_FDB_PORT), self.walk(BRIDGE_FDB_STATUS), False
        seen = set()
        for suffix, bridge_port in fdb.items():
            parts = suffix.split(".")
            if len(parts) < 6 or status.get(suffix, 3) not in (1, 3):   # other or learned
                continue
            mac = ":".join(f"{int(x):02x}" for x in parts[-6:])
            vlan = int(parts[0]) if vlan_aware and len(parts) == 7 else None
            n = port_for(bridge_port)
            if n is None or mac in own_macs or (n, mac) in seen:
                continue
            seen.add((n, mac))
            by_index[n].macs.append((mac, vlan))

        neighbors = []
        lldp = self.walk(LLDP_REM)
        chassis, port_ids, port_desc = column(lldp, 5), column(lldp, 7), column(lldp, 8)
        sys_name, sys_desc = column(lldp, 9), column(lldp, 10)
        for key, cid in chassis.items():
            parts = key.split(".")
            n = port_for(parts[1]) if len(parts) == 3 else None
            if n is None:
                continue
            neighbors.append(Neighbor(
                port=n, chassis_id=text(cid),
                port_id=text(port_ids.get(key)) or text(port_desc.get(key)),
                name=text(sys_name.get(key)), description=text(sys_desc.get(key)),
            ))

        uptime = system.get("3.0")
        return Snapshot(
            ports=ports, neighbors=neighbors,
            uptime=uptime // 100 if isinstance(uptime, int) else None,
            description=text(system.get("1.0")),
            mac=mac_str(self.walk(BRIDGE_ADDRESS).get("0")),
        )
