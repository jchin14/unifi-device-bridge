"""CPU and memory for UniFi's device overview.

Real numbers when the switch reports them (SNMP HOST-RESOURCES-MIB). Many switches, like
TP-Link Easy Smart, have no way to read them: then cpu_percent and mem_percent from the config
are shown as flat lines, or nothing if they aren't set. Nothing is ever made up.
"""

MEM_TOTAL = 128 * 1024 * 1024   # memory size sent when the switch doesn't report one


def fixed(cfg, key):
    """A percentage from the config, or None if unset."""
    val = cfg.get(key)
    if val is None:
        return None
    if not isinstance(val, (int, float)) or not 0 <= val <= 100:
        raise ValueError(f"{key} must be a number from 0 to 100")
    return float(val)


def fill(snapshot, cfg):
    """Use the configured CPU and memory where the switch didn't report its own."""
    if snapshot.cpu is None:
        snapshot.cpu = fixed(cfg, "cpu_percent")
    if snapshot.mem is None:
        snapshot.mem = fixed(cfg, "mem_percent")
    return snapshot


def sys_stats(snapshot):
    """The sys_stats block a UniFi device sends: load averages and memory in bytes (what's known)."""
    out = {}
    if snapshot.cpu is not None:
        load = f"{snapshot.cpu / 100:.2f}"
        out.update({"loadavg_1": load, "loadavg_5": load, "loadavg_15": load})
    if snapshot.mem is not None:
        total = snapshot.mem_total or MEM_TOTAL
        out.update({"mem_total": total, "mem_used": int(total * snapshot.mem / 100), "mem_buffer": 0})
    return out
