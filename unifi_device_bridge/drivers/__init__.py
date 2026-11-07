from ..model import DriverError


def make_driver(cfg, port_count):
    name = cfg.get("driver", "snmp")
    if name == "snmp":
        from .snmp import SnmpDriver
        return SnmpDriver(cfg.get("snmp", {}), port_count)
    if name == "easysmart":
        from .easysmart import EasySmartDriver
        return EasySmartDriver(cfg.get("easysmart", {}), port_count)
    if name == "fake":
        from .fake import FakeDriver
        return FakeDriver(cfg.get("fake", {}), port_count)
    raise DriverError(f"unknown driver {name!r} (use snmp, easysmart or fake)")
