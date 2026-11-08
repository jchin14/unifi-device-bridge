import unittest

from unifi_device_bridge import inform
from unifi_device_bridge.__main__ import network_from_system_cfg, ports_from_system_cfg, save_host, vlans_from_system_cfg
from unifi_device_bridge.drivers.easysmart import parse_ip, parse_stats, parse_vlans
from unifi_device_bridge.drivers.snmp import column, host_resources, parse_walk

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


    def test_host_resources(self):
        cpu = parse_walk('.1.3.6.1.2.1.25.3.3.1.2.196608 = INTEGER: 10\n'
                         '.1.3.6.1.2.1.25.3.3.1.2.196609 = INTEGER: 20\n', "1.3.6.1.2.1.25.3.3.1.2")
        storage = parse_walk('.1.3.6.1.2.1.25.2.3.1.2.1 = OID: .1.3.6.1.2.1.25.2.1.2\n'
                             '.1.3.6.1.2.1.25.2.3.1.4.1 = INTEGER: 1024 Bytes\n'
                             '.1.3.6.1.2.1.25.2.3.1.5.1 = INTEGER: 131072\n'
                             '.1.3.6.1.2.1.25.2.3.1.6.1 = INTEGER: 65536\n', "1.3.6.1.2.1.25.2.3.1")
        self.assertEqual(host_resources(cpu, storage), (15.0, 50.0, 128 * 1024 * 1024))
        self.assertEqual(host_resources({}, {}), (None, None, None))


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


class VlanTest(unittest.TestCase):
    def test_parse_vlans(self):
        q = 'qvlan_ds = {\nstate:1,\nportNum:8,\nvids:[\n1,20\n],\ncount:2,\nmaxVids:32,\nnames:[\n"Default","iot"\n],\ntagMbrs:[\n0x0,0x80\n],\nuntagMbrs:[\n0xff,0x3\n],\nlagIds:[0],lagMbrs:[0]\n};'
        pv = 'pvid_ds = {\nstate:1,\nportNum:8,\nvids:[1,20],\npvids:[\n20,20,1,1,1,1,1,1\n],\nlagIds:[0]\n};'
        v = parse_vlans(q, pv)
        self.assertTrue(v["enabled"])
        self.assertEqual(v["vlans"][20], {"name": "iot", "tagged": {8}, "untagged": {1, 2}})
        self.assertEqual(v["pvid"][1], 20)

    def test_system_cfg(self):
        cfg = ("switch.port.1.name=a\nswitch.port.2.name=b\nswitch.port.3.name=c\n"
               "switch.vlan.1.id=1\nswitch.vlan.1.mode=untagged\nswitch.vlan.1.port.3.mode=exclude\n"
               "switch.vlan.2.id=20\nswitch.vlan.2.mode=exclude\nswitch.vlan.2.port.3.mode=untagged\n"
               "switch.vlan.2.port.1.mode=tagged\n")
        v = vlans_from_system_cfg(cfg)
        self.assertEqual(v["vlans"][1]["untagged"], {1, 2})
        self.assertEqual(v["vlans"][20], {"tagged": {1}, "untagged": {3}})
        self.assertEqual(v["pvid"], {1: 1, 2: 1, 3: 20})
        self.assertIsNone(vlans_from_system_cfg("switch.port.1.name=a\n"))
        # No mode for VLAN 30: tagged on every port by default
        v = vlans_from_system_cfg("switch.port.1.name=a\nswitch.port.2.name=b\nswitch.vlan.1.id=1\n"
                                  "switch.vlan.1.mode=untagged\nswitch.vlan.2.id=30\n")
        self.assertEqual(v["vlans"][30]["tagged"], {1, 2})


class EasySmartSessionTest(unittest.TestCase):
    def test_pause_when_web_ui_used(self):
        from unittest import mock
        from unifi_device_bridge.drivers.easysmart import EasySmartDriver
        from unifi_device_bridge.model import DriverError
        drv = EasySmartDriver({"host": "x", "password": "p"}, 2)
        calls = []
        replies = iter(["", "ok", "ok", "<script>var logonInfo = new Array(0,0,0);</script>"])
        with mock.patch.object(drv, "_get", lambda path, data=None: calls.append(path) or next(replies)):
            self.assertEqual(drv.page("/a"), "ok")       # logs in once
            self.assertEqual(drv.page("/b"), "ok")       # reuses the session
            with self.assertRaises(DriverError):
                drv.page("/c")                          # session lost: pause, no re-login
            with self.assertRaises(DriverError):
                drv.page("/d")                          # still paused, no request sent
        self.assertEqual(calls, ["/logon.cgi", "/a", "/b", "/c"])
        drv.resume()
        self.assertEqual(drv.paused_until, 0)


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


class StatsTest(unittest.TestCase):
    def test_fill(self):
        from unifi_device_bridge import report, stats
        from unifi_device_bridge.model import Port, Snapshot
        cfg = {"device_ip": "1.2.3.4", "cpu_percent": 21, "mem_percent": 67}
        for _ in range(3):   # flat: the same every poll
            snap = stats.fill(Snapshot(ports=[Port(index=1)]), cfg)
            self.assertEqual((snap.cpu, snap.mem), (21.0, 67.0))
        body = report.build(snap, {"mac": MAC}, cfg, "x", 0)
        self.assertEqual((body["system-stats"]["cpu"], body["system-stats"]["mem"]), ("21.0", "67.0"))
        self.assertEqual(body["sys_stats"]["mem_used"], int(stats.MEM_TOTAL * 0.67))
        real = stats.fill(Snapshot(ports=[], cpu=3.0), cfg)   # the switch's own number wins
        self.assertEqual((real.cpu, real.mem), (3.0, 67.0))
        unset = stats.fill(Snapshot(ports=[]), {"device_ip": "1.2.3.4"})
        self.assertIsNone(unset.cpu)
        self.assertNotIn("system-stats", report.build(unset, {"mac": MAC}, {"device_ip": "1.2.3.4"}, "x", 0))
        only_cpu = report.build(stats.fill(Snapshot(ports=[]), {"cpu_percent": 5}), {"mac": MAC},
                                {"device_ip": "1.2.3.4"}, "x", 0)
        self.assertNotIn("mem", only_cpu["system-stats"])
        with self.assertRaises(ValueError):
            stats.fill(Snapshot(ports=[]), {"cpu_percent": 150})


class EasySmartBytesTest(unittest.TestCase):
    def test_estimate(self):
        from unittest import mock
        from unifi_device_bridge.drivers.easysmart import EasySmartDriver
        html = "<script>var max_port_num = 2;\nvar all_info = {\nstate:[1,1],\nlink_status:[6,0],\npkts:[10,0,20,1,0,0,0,0]\n};</script>"
        drv = EasySmartDriver({"host": "x", "password": "p", "bytes_per_packet": 1000}, 2)
        pages = {"/PortStatisticsRpm.htm": html, "/SystemInfoRpm.htm": ""}

        with mock.patch.object(drv, "page", pages.get):
            p = drv.poll().ports[0]
        self.assertEqual((p.tx_bytes, p.rx_bytes), (10_000, 20_000))


class UplinkTest(unittest.TestCase):
    def test_uplink_neighbor(self):
        from unifi_device_bridge import report
        from unifi_device_bridge.model import Port, Snapshot
        cfg = {"model": "US8P60", "device_ip": "1.2.3.4", "uplink_port": 8,
               "uplink_neighbor": {"mac": "AA:BB:CC:DD:EE:FF", "name": "Office", "port": "Port 8"}}
        snap = Snapshot(ports=[Port(index=n, name=f"Port {n}", up=True) for n in range(1, 9)])
        body = report.build(snap, {"mac": MAC}, cfg, "x", 0)
        self.assertEqual(body["uplink"], "eth0")
        (eth,) = body["if_table"]
        self.assertEqual((eth["name"], eth["uplink_mac"], eth["uplink_remote_port"], eth["port_idx"]),
                         ("eth0", "aa:bb:cc:dd:ee:ff", 8, 8))


class VirtualTest(unittest.TestCase):
    def test_switch(self):
        from unifi_device_bridge import report
        from unifi_device_bridge.drivers import make_driver
        cfg = {"driver": "virtual", "name": "A", "model": "US8P60", "device_ip": "1.2.3.4", "uplink_port": 1,
               "virtual": {"up_ports": [1, 5]}}
        drv = make_driver(cfg, 8)
        drv.set_poe(5, False)
        snap = drv.poll()
        self.assertEqual([p.index for p in snap.ports if p.up], [1, 5])
        self.assertFalse(snap.ports[4].poe_enabled)
        body = report.build(snap, {"mac": MAC}, cfg, "x", 0)
        self.assertNotIn("mac_table", body["port_table"][0])
        self.assertNotIn("mac_table", body["port_table"][1])
        drv.set_port_enabled(5, False)
        self.assertFalse(drv.poll().ports[4].up)

    def test_ap(self):
        from unifi_device_bridge import report
        from unifi_device_bridge.drivers import make_driver
        cfg = {"driver": "virtual", "name": "AP", "model": "UAL6", "device_ip": "1.2.3.4"}
        self.assertEqual(report.model_info(cfg)[2], 1)
        body = report.build(make_driver(cfg, 1).poll(), {"mac": MAC}, cfg, "x", 0)
        self.assertEqual(body["model"], "UAL6")
        self.assertEqual([r["radio"] for r in body["radio_table"]], ["ng", "na"])
        self.assertEqual(len(body["port_table"]), 1)
        self.assertEqual(body["uplink"]["type"], "wire")

    def test_wifi7(self):
        from unifi_device_bridge import report
        from unifi_device_bridge.drivers import make_driver
        for model, code, bands in (("U7-Pro-XGS", "UAPA6A4", ["ng", "na", "6e"]),
                                   ("e7-audience", "UAPA699", ["na", "6e"])):
            cfg = {"driver": "virtual", "name": model, "model": model, "device_ip": "1.2.3.4",
                   "virtual": {"channels": [149]}}
            body = report.build(make_driver(cfg, 1).poll(), {"mac": MAC}, cfg, "x", 0)
            self.assertEqual(body["model"], code)
            self.assertEqual([r["radio"] for r in body["radio_table"]], bands)
            self.assertEqual(body["uplink"]["speed"], 10000)
            self.assertTrue(all(r["is_11be"] for r in body["radio_table"]))
        self.assertEqual(body["radio_table"][0]["channel"], 149)
        self.assertIn(body["radio_table"][1]["channel"], (37, 69, 101, 133))

    def test_device_list(self):
        from unifi_device_bridge.__main__ import device_configs
        cfg = {"inform_url": "x", "driver": "virtual", "state_file": "/s/state.json",
               "device": [{"name": "Core Switch", "model": "US24"}, {"name": "AP", "model": "UAL6"}]}
        cfgs = device_configs(cfg)
        self.assertEqual([c["state_file"] for c in cfgs], ["/s/core-switch.json", "/s/ap.json"])
        self.assertEqual(cfgs[1]["driver"], "virtual")
        with self.assertRaises(ValueError):
            device_configs({"device": [{"name": "A"}, {"name": "a"}]})
        self.assertEqual(device_configs({"name": "x"}), [{"name": "x"}])


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
