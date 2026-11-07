import unittest

from unifi_device_bridge import inform
from unifi_device_bridge.__main__ import network_from_system_cfg, ports_from_system_cfg, save_host
from unifi_device_bridge.drivers.easysmart import parse_ip, parse_stats
from unifi_device_bridge.drivers.snmp import column, parse_walk

MAC = "02:11:22:33:44:55"


class InformTest(unittest.TestCase):
    def test_roundtrip(self):
        for gcm in (False, True):
            pkt = inform.encode({"a": 1}, MAC, inform.DEFAULT_KEY, gcm=gcm)
            self.assertEqual(inform.decode(pkt, inform.DEFAULT_KEY), (MAC, {"a": 1}))

    def test_wrong_key(self):
        pkt = inform.encode({"a": 1}, MAC, inform.DEFAULT_KEY, gcm=True)
        with self.assertRaises(inform.InformError):
            inform.decode(pkt, b"x" * 16)


class SnmpParseTest(unittest.TestCase):
    def test_walk(self):
        out = ('.1.3.6.1.2.1.31.1.1.1.1.3 = STRING: "gi1"\n'
               '.1.3.6.1.2.1.31.1.1.1.6.3 = Counter64: 12345\n'
               '.1.3.6.1.2.1.31.1.1.1.15.3 = Gauge32: 1000\n')
        t = parse_walk(out, "1.3.6.1.2.1.31.1.1.1")
        self.assertEqual(column(t, 1), {"3": "gi1"})
        self.assertEqual(column(t, 6), {"3": 12345})

    def test_timeticks_and_hex(self):
        out = ('.1.3.6.1.2.1.1.3.0 = Timeticks: (123400) 0:20:34.00\n'
               '.1.3.6.1.2.1.2.2.1.6.2 = Hex-STRING: DC A6 32 E1 15 2D \n')
        self.assertEqual(parse_walk(out, "1.3.6.1.2.1.1"), {"3.0": 123400})
        self.assertEqual(parse_walk(out, "1.3.6.1.2.1.2.2.1"), {"6.2": bytes.fromhex("dca632e1152d")})


class EasySmartTest(unittest.TestCase):
    def test_parse(self):
        html = """<script>var max_port_num = 3;
var all_info = {
state:[1,1,0,0,0],
link_status:[6,0,5,0,0],
pkts:[10,0,20,1,0,0,0,0,7,0,8,0,0,0]
};</script>"""
        rows = parse_stats(html)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], (True, 6, 10, 0, 20, 1))
        self.assertEqual(rows[2], (False, 5, 7, 0, 8, 0))

    def test_parse_ip(self):
        html = 'var ip_ds = {state:0,vlan:1,ipStr:["10.0.0.5"],netmaskStr:["255.255.255.0"],gatewayStr:["10.0.0.1"]};'
        self.assertEqual(parse_ip(html), {"dhcp": False, "ip": "10.0.0.5", "netmask": "255.255.255.0",
                                          "gateway": "10.0.0.1"})


class NetworkTest(unittest.TestCase):
    def test_system_cfg(self):
        self.assertEqual(network_from_system_cfg("netconf.1.ip=0.0.0.0\ndhcpc.1.status=enabled\n"), {"dhcp": True})
        cfg = "netconf.1.ip=10.0.0.5\nnetconf.1.netmask=255.255.255.0\ndhcpc.status=disabled\nroute.1.gateway=10.0.0.1\n"
        self.assertEqual(network_from_system_cfg(cfg)["ip"], "10.0.0.5")
        self.assertIsNone(network_from_system_cfg("netconf.1.ip=0.0.0.0\n"))

    def test_ports_cfg(self):
        cfg = "switch.port.1.name=Port 1\nswitch.port.5.poe=off\nswitch.port.6.poe=auto\nswitch.port.2.status=disabled\n"
        p = ports_from_system_cfg(cfg)
        self.assertEqual(p[1], {"enabled": True, "poe": None})
        self.assertEqual(p[2]["enabled"], False)
        self.assertEqual((p[5]["poe"], p[6]["poe"]), (False, True))


class ReportTest(unittest.TestCase):
    def test_models_and_uptime(self):
        from unifi_device_bridge import report
        from unifi_device_bridge.model import Port, Snapshot
        snap = Snapshot(ports=[Port(index=n, up=True, speed=1000, macs=[("aa:aa:aa:aa:aa:0%d" % n, None)])
                               for n in range(1, 17)])
        cfg = {"model": "US16P150", "device_ip": "1.2.3.4", "uplink_port": 1}
        body = report.build(snap, {"mac": MAC}, cfg, "x", 0, {(2, "aa:aa:aa:aa:aa:02"): 0})
        self.assertEqual(len(body["port_table"]), 16)
        self.assertTrue(body["port_table"][15]["port_poe"])
        self.assertEqual(body["port_table"][0]["mac_table"], [])
        self.assertGreater(body["port_table"][1]["mac_table"][0]["uptime"], 0)


class SaveHostTest(unittest.TestCase):
    def test_save_host(self):
        import os, tempfile
        fd, path = tempfile.mkstemp()
        os.write(fd, b'# c\n[snmp]\nhost = "1.1.1.1"\n\n[easysmart]\nhost = "1.1.1.1"  # x\npassword = "p"\n')
        os.close(fd)
        self.assertTrue(save_host(path, "easysmart", "10.0.0.9"))
        with open(path) as f:
            text = f.read()
        os.remove(path)
        self.assertIn('[snmp]\nhost = "1.1.1.1"', text)
        self.assertIn('[easysmart]\nhost = "10.0.0.9"\npassword', text)


if __name__ == "__main__":
    unittest.main()
